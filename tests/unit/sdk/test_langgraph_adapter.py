"""Design constraint 1: the adapter binds, or it says so — never partial capture in silence.

The file is organised around the two halves of that claim. The first half asserts that the
five PRD §8.3 bindings actually produce the events they promise. The second half breaks each
binding deliberately — a changed callback signature, an unrecognised channel type, a node
return the adapter cannot read — and asserts that each one produces an `instrumentation_gap`
event and a loud failure rather than a quieter log.
"""

from __future__ import annotations

import warnings

import pytest

import agentdx
from agentdx.events.schema import EventType
from agentdx.events.validators import validate_log
from agentdx.sdk.generic import InstrumentationGapWarning, use_run
from agentdx.sdk.langgraph import (
    CHANNEL_REDUCERS,
    accepts_input_and_config,
    bind,
    detect_reducer,
    is_internal_channel,
    qualified_name,
)
from tests.unit.sdk.fakes import make_context
from tests.unit.sdk.graphs import (
    build_bulk_reader,
    build_fanout,
    build_pipeline,
    build_sync_pipeline,
    build_with_subgraph,
)

# ---------------------------------------------------------------------------------------
# The bindings do what PRD §8.3 says
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_line_instrumentation_produces_a_valid_event_log() -> None:
    context, recorder = make_context()

    graph = agentdx.instrument(build_pipeline(), name="pipeline", context=context)

    await graph.ainvoke({"task": "ship it"})

    validate_log(recorder.events)
    kinds = [event.payload["kind"] for event in recorder.of_type(EventType.SPAN_START)]
    assert kinds == ["agent_step", "agent_step", "agent_step"]
    agents = [event.agent_id for event in recorder.of_type(EventType.SPAN_START)]
    assert agents == ["planner", "coder", "reviewer"]


@pytest.mark.asyncio
async def test_a_sync_node_graph_produces_the_same_event_types() -> None:
    context, recorder = make_context()
    graph = agentdx.instrument(build_sync_pipeline(), name="sync", context=context)

    graph.invoke({"task": "ship it"})

    validate_log(recorder.events)
    assert [event.agent_id for event in recorder.of_type(EventType.SPAN_START)] == [
        "planner",
        "coder",
    ]


@pytest.mark.asyncio
async def test_state_writes_are_attributed_to_the_node_that_made_them() -> None:
    context, recorder = make_context()
    graph = agentdx.instrument(build_pipeline(), name="pipeline", context=context)

    await graph.ainvoke({"task": "ship it"})

    writes = [
        (event.agent_id, event.payload["key"], event.payload["reducer"])
        for event in recorder.of_type(EventType.STATE_WRITE)
    ]
    assert ("planner", "plan", None) in writes
    assert ("coder", "drafts", "operator.add") in writes
    assert ("reviewer", "review", None) in writes


@pytest.mark.asyncio
async def test_a_reduced_channel_records_its_reducer_on_every_write() -> None:
    # PRD §8.3: "Without this, AgentDX would report a false positive on almost every real
    # LangGraph application — this is the single highest-risk false-positive source."
    context, recorder = make_context()
    graph = agentdx.instrument(build_fanout(), name="fanout", context=context)

    await graph.ainvoke({"task": "t"})

    reduced = [
        event.payload["reducer"]
        for event in recorder.of_type(EventType.STATE_WRITE)
        if event.payload["key"] == "drafts"
    ]
    assert reduced == ["operator.add", "operator.add"]


@pytest.mark.asyncio
async def test_prev_value_hash_makes_a_concurrent_overwrite_visible() -> None:
    context, recorder = make_context()
    graph = agentdx.instrument(build_fanout(), name="fanout", context=context)

    await graph.ainvoke({"task": "t"})

    drafts = [
        event.payload
        for event in recorder.of_type(EventType.STATE_WRITE)
        if event.payload["key"] == "drafts"
    ]
    assert len(drafts) == 2
    assert drafts[0]["prev_value_hash"] == drafts[1]["prev_value_hash"], (
        "two writers in one superstep saw the same prior value; that identity is exactly "
        "what a lost-update detector keys on"
    )


@pytest.mark.asyncio
async def test_node_reads_are_recorded_once_per_key() -> None:
    context, recorder = make_context()
    graph = agentdx.instrument(build_pipeline(), name="pipeline", context=context)

    await graph.ainvoke({"task": "ship it"})

    reads = [
        (event.agent_id, event.payload["key"]) for event in recorder.of_type(EventType.STATE_READ)
    ]
    assert ("planner", "task") in reads
    assert ("coder", "plan") in reads
    assert len(reads) == len(set(reads)), "a key must be recorded once per node invocation"


@pytest.mark.asyncio
async def test_edge_traversal_produces_one_send_and_one_recv_per_handoff() -> None:
    context, recorder = make_context()
    graph = agentdx.instrument(build_pipeline(), name="pipeline", context=context)

    await graph.ainvoke({"task": "ship it"})

    sends = recorder.of_type(EventType.MESSAGE_SEND)
    recvs = recorder.of_type(EventType.MESSAGE_RECV)
    assert [event.payload["edge"] for event in sends] == ["planner->coder", "coder->reviewer"]
    assert len(sends) == len(recvs) == 2
    for send, recv in zip(sends, recvs, strict=True):
        assert recv.payload["message_id"] == send.payload["message_id"]
        assert send.seq in recv.causal_parents, (
            "a message_recv must name its send as a causal parent — messages are the only "
            "carrier of happens-before between agents (PRD §14.3)"
        )


@pytest.mark.asyncio
async def test_the_original_graph_is_left_uninstrumented() -> None:
    # Instrumentation is not a one-way door: the object the user compiled keeps working, and
    # keeps working *uninstrumented*, so a second run without AgentDX needs no reconstruction.
    context, recorder = make_context()
    original = build_pipeline()
    instrumented = agentdx.instrument(original, name="pipeline", context=context)

    assert instrumented.graph is not original
    assert original.nodes["planner"] is not instrumented.graph.nodes["planner"]

    await original.ainvoke({"task": "t"})
    assert recorder.events == [], "the original graph must record nothing"

    await instrumented.ainvoke({"task": "t"})
    assert recorder.events != []


@pytest.mark.asyncio
async def test_the_instrumented_graph_delegates_unknown_attributes() -> None:
    context, _ = make_context()
    graph = agentdx.instrument(build_pipeline(), name="pipeline", context=context)
    assert graph.nodes.keys() == graph.graph.nodes.keys()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------
# Reducer detection and classification
# ---------------------------------------------------------------------------------------


def test_operator_add_is_named_the_way_the_prd_names_it() -> None:
    import operator

    assert qualified_name(operator.add) == "operator.add"


def test_every_channel_of_the_pinned_langgraph_is_in_the_table() -> None:
    # ADR-003 pins >=1.2,<1.3. A 1.3 bump that adds a channel class lands here first, as a
    # fatal gap on a user channel, rather than as a silently missing reducer.
    import langgraph.channels as channels

    exported = {
        name
        for name in dir(channels)
        if isinstance(getattr(channels, name), type) and not name.startswith("_")
    }
    missing = sorted(exported - set(CHANNEL_REDUCERS) - {"BaseChannel"})
    assert missing == [], f"unmapped channel classes in the pinned LangGraph: {missing}"


def test_reducer_detection_reads_the_instance_not_the_class() -> None:
    graph = build_pipeline()
    reducer, recognised = detect_reducer(graph.channels["drafts"])
    assert recognised
    assert reducer == "operator.add"
    reducer, recognised = detect_reducer(graph.channels["plan"])
    assert recognised
    assert reducer is None


def test_an_unknown_channel_class_is_reported_as_unrecognised() -> None:
    class SomeFutureChannel:
        pass

    assert detect_reducer(SomeFutureChannel()) == (None, False)


def test_a_recognised_class_whose_instance_reducer_is_unreadable_is_not_recognised() -> None:
    # OP-2 finding D2. `BinaryOperatorAggregate` and `DeltaChannel` are in the table, but
    # their reducer is a *property of the instance*, not of the class. If a LangGraph 1.3
    # renames the attribute the reducer lives under, the table entry alone must not be
    # allowed to answer for it: returning `(None, True)` would mark a reducing channel as
    # non-reducing, and PRD §8.3 calls that the single highest-risk false-positive source
    # in the product. It must land in the fatal `instrumentation_gap` path instead, exactly
    # as a channel class nobody has ever heard of does.
    class BinaryOperatorAggregate:
        """A future LangGraph in which the reducer moved to a renamed attribute."""

        def __init__(self) -> None:
            self.combine = sum  # not `operator`, not `reducer`

    assert detect_reducer(BinaryOperatorAggregate()) == (None, False)


def test_a_delta_channel_reducer_is_still_read_from_the_instance() -> None:
    # The other half of the sentinel: the classes it guards must still resolve normally
    # when the attribute *is* where the pinned minor puts it.
    import operator

    from langgraph.channels import BinaryOperatorAggregate, DeltaChannel

    assert detect_reducer(BinaryOperatorAggregate(list, operator.add)) == ("operator.add", True)
    reducer, recognised = detect_reducer(DeltaChannel(operator.add, list))
    assert recognised
    assert reducer == "operator.add"


def test_control_plane_channels_are_classified_as_internal() -> None:
    assert is_internal_channel("__start__")
    assert is_internal_channel("branch:to:coder")
    assert not is_internal_channel("drafts")


def test_the_signature_probe_accepts_input_and_config() -> None:
    class Good:
        def ainvoke(self, node_input: object, config: object = None) -> object:
            return None

    class Changed:
        def ainvoke(self, node_input: object) -> object:
            return None

    class Varargs:
        def ainvoke(self, *args: object, **kwargs: object) -> object:
            return None

    assert accepts_input_and_config(Good.ainvoke)
    assert not accepts_input_and_config(Changed.ainvoke)
    assert accepts_input_and_config(Varargs.ainvoke)
    assert not accepts_input_and_config(None)


# ---------------------------------------------------------------------------------------
# The failure paths — design constraint 1
# ---------------------------------------------------------------------------------------


class _ChangedSignatureBound:
    """A node callable whose `ainvoke` lost its `config` parameter — upstream version drift."""

    def invoke(self, node_input: object, config: object = None) -> object:
        return {}

    async def ainvoke(self, node_input: object) -> object:
        return {}


class _FakeNode:
    """The shape of a LangGraph `PregelNode`, enough for the adapter to probe."""

    def __init__(self, bound: object) -> None:
        self.bound = bound
        self.triggers: list[str] = []

    def copy(self, update: dict[str, object]) -> _FakeNode:
        return _FakeNode(update.get("bound", self.bound))


class _FakeChannel:
    """A channel class the adapter's reducer table has never heard of."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = None

    def update(self, values: object) -> bool:
        return True

    def checkpoint(self) -> object:
        return self.value


class _FakeGraph:
    """The smallest object `instrument()` accepts, so a binding can be broken in isolation."""

    def __init__(self, nodes: dict[str, object], channels: dict[str, object]) -> None:
        self.nodes = nodes
        self.channels = channels

    async def ainvoke(self, node_input: object, config: object = None) -> object:
        return {}


def test_a_changed_callback_signature_is_fatal_and_recorded() -> None:
    context, recorder = make_context()
    graph = _FakeGraph({"coder": _FakeNode(_ChangedSignatureBound())}, {"plan": _FakeChannel()})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(agentdx.InstrumentationError) as raised:
            agentdx.instrument(graph, name="drifted", context=context)

    gaps = recorder.payloads(EventType.INSTRUMENTATION_GAP)
    constructs = {str(gap["construct"]) for gap in gaps}
    assert "node_callback_signature" in constructs
    assert any("no longer accept (input, config)" in str(gap["reason"]) for gap in gaps)
    assert "E-INSTR-002" in str(raised.value)
    assert "will not record a partially-captured run" in str(raised.value)
    assert any(issubclass(w.category, InstrumentationGapWarning) for w in caught)


def test_an_unrecognised_user_channel_is_fatal() -> None:
    context, recorder = make_context()

    class _WorkingBound:
        def invoke(self, node_input: object, config: object = None) -> object:
            return {}

        async def ainvoke(self, node_input: object, config: object = None) -> object:
            return {}

    graph = _FakeGraph({"coder": _FakeNode(_WorkingBound())}, {"drafts": _FakeChannel()})

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        with pytest.raises(agentdx.InstrumentationError):
            agentdx.instrument(graph, name="unknown_channel", context=context)

    gaps = recorder.payloads(EventType.INSTRUMENTATION_GAP)
    assert any(gap["construct"] == "channel_type" for gap in gaps)
    assert any("reducer is unknown" in str(gap["reason"]) for gap in gaps)


def test_an_unrecognised_control_plane_channel_is_a_gap_but_not_fatal() -> None:
    context, recorder = make_context()

    class _WorkingBound:
        def invoke(self, node_input: object, config: object = None) -> object:
            return {}

        async def ainvoke(self, node_input: object, config: object = None) -> object:
            return {}

    graph = _FakeGraph({"coder": _FakeNode(_WorkingBound())}, {"__pregel_future__": _FakeChannel()})

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        instrumented = agentdx.instrument(graph, name="future", context=context)

    constructs = [gap.construct for gap in instrumented.gaps]
    assert constructs == ["graph_copy", "channel_type", "edge_traversal"]
    assert all(not gap.fatal for gap in instrumented.gaps)
    gaps = recorder.payloads(EventType.INSTRUMENTATION_GAP)
    assert {str(gap["construct"]) for gap in gaps} == set(constructs)


def test_a_non_graph_target_names_the_decorator_api() -> None:
    with pytest.raises(agentdx.UnsupportedTargetError) as caught:
        agentdx.instrument(object(), name="not_a_graph")
    assert "E-INSTR-006" in str(caught.value)
    assert "@agentdx.agent" in str(caught.value)


@pytest.mark.asyncio
async def test_an_uninterpretable_node_return_is_reported_rather_than_dropped() -> None:
    context, recorder = make_context()

    class _OpaqueResult:
        __slots__ = ()

    class _OpaqueBound:
        def invoke(self, node_input: object, config: object = None) -> object:
            return _OpaqueResult()

        async def ainvoke(self, node_input: object, config: object = None) -> object:
            return _OpaqueResult()

    graph = _FakeGraph({"coder": _FakeNode(_OpaqueBound())}, {})
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        adapter, working = bind(graph, name="opaque")
    adapter.reset(context)

    with warnings.catch_warnings(record=True), use_run(context):
        warnings.simplefilter("always")
        node = working.nodes["coder"]  # type: ignore[attr-defined]
        await node.bound.ainvoke({"task": "t"}, None)

    gaps = recorder.payloads(EventType.INSTRUMENTATION_GAP)
    assert any(gap["construct"] == "node_output" for gap in gaps)


@pytest.mark.asyncio
async def test_invoking_an_instrumented_graph_outside_a_run_is_an_error() -> None:
    graph = agentdx.instrument(build_pipeline(), name="pipeline")
    with pytest.raises(agentdx.RunContextError):
        await graph.ainvoke({"task": "t"})


# ---------------------------------------------------------------------------------------
# The silent-capture regressions found by the independent OP-2 audit
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["dict", "splat"])
@pytest.mark.asyncio
async def test_a_bulk_read_of_the_whole_state_is_recorded(style: str) -> None:
    # OP-2 finding D3. `dict(state)` and `{**state}` take CPython's C-level dict-merge fast
    # path when the argument is an actual `dict` subclass, and that path reads the object's
    # internal table directly rather than calling `__getitem__` or `keys()`. A recording
    # view that subclassed `dict` therefore recorded *nothing* for the single most common
    # way a real node reads its state, and the log contained no `state_read` at all — a
    # capture hole with no gap event, which is the one failure mode this SDK exists to
    # prevent.
    context, recorder = make_context()
    graph = agentdx.instrument(build_bulk_reader(style), name=f"bulk_{style}", context=context)

    await graph.ainvoke({"task": "ship it"})

    reads = [event.payload["key"] for event in recorder.of_type(EventType.STATE_READ)]
    assert reads != [], (
        f"a node that read its whole state with {style} produced no state_read events; the "
        f"recording view was bypassed by a C-level fast path"
    )
    assert "task" in reads


@pytest.mark.asyncio
async def test_two_nodes_sharing_an_agent_id_do_not_share_a_clock_slot() -> None:
    # OP-2 finding D4. `agent_from` may map several nodes onto one agent id — that is what
    # PRD §8.2 item 1 offers it for. Two such nodes in one superstep are concurrent by
    # Pregel's own semantics, so writing both onto one vector-clock slot imposes a total
    # order the run did not have and hides every race between them (PRD §14.2). Their
    # happens-before, where there is one, is carried by the message events of binding 4,
    # never by slot identity.
    context, recorder = make_context()
    graph = agentdx.instrument(
        build_fanout(),
        name="fanout",
        context=context,
        agent_from=lambda node: "worker" if node.startswith("worker") else node,
    )

    await graph.ainvoke({"task": "t"})

    writes = [
        event
        for event in recorder.of_type(EventType.STATE_WRITE)
        if event.payload["key"] == "drafts"
    ]
    assert len(writes) == 2
    assert writes[0].agent_id == writes[1].agent_id == "worker"
    assert writes[0].clock_slot != writes[1].clock_slot, (
        "two concurrent nodes mapped to one agent id collapsed onto a single clock slot"
    )

    # And the write's slot is the slot of the span it happened inside, rather than the bare
    # agent id — a write stamped onto a different slot from its own span is worse than no
    # slot at all, because it is a plausible-looking one.
    spans = {event.span_id: event.clock_slot for event in recorder.of_type(EventType.SPAN_START)}
    for write in writes:
        assert write.clock_slot == spans[write.span_id]


@pytest.mark.asyncio
async def test_a_message_send_carries_the_producers_clock_slot() -> None:
    # The send half of D4: it is attributed to the *producing* node, whose scope has already
    # exited by the time the consumer records the handoff, so the producer's slot has to be
    # carried forward rather than re-derived from the agent id.
    context, recorder = make_context()
    graph = agentdx.instrument(
        build_pipeline(),
        name="pipeline",
        context=context,
        agent_from=lambda node: "worker",
    )

    await graph.ainvoke({"task": "t"})

    slots = {event.clock_slot for event in recorder.of_type(EventType.SPAN_START)}
    assert len(slots) == 3, "three nodes on one agent id need three slots"

    spans = {event.span_id: event.clock_slot for event in recorder.of_type(EventType.SPAN_START)}
    for send in recorder.of_type(EventType.MESSAGE_SEND):
        assert send.span_id is not None
        assert send.clock_slot == spans[send.span_id]


def test_a_mounted_subgraph_is_a_fatal_gap_rather_than_one_opaque_agent() -> None:
    # OP-2 finding D6. A compiled subgraph mounted as a node runs its own nodes, and the
    # node-binding walk never sees them: no spans, no state events, no handoffs for
    # anything inside it. Recording the whole subgraph as one agent produces a log that is
    # structurally incomplete while looking complete, which is the condition PRD §8.3
    # reserves `E-INSTR-002` for. Full path-qualified recursive subgraph support is real
    # additional scope and is not attempted here; refusing the graph loudly is.
    context, recorder = make_context()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(agentdx.InstrumentationError) as raised:
            agentdx.instrument(build_with_subgraph(), name="parent", context=context)

    assert "planning" in str(raised.value)
    assert "E-INSTR-002" in str(raised.value)
    gaps = recorder.payloads(EventType.INSTRUMENTATION_GAP)
    subgraph_gaps = [gap for gap in gaps if gap["construct"] == "subgraph"]
    assert subgraph_gaps != [], "the gap must be in the log, not only in the exception"
    assert "parent.planning" in str(subgraph_gaps[0]["location"])
    assert any(issubclass(w.category, InstrumentationGapWarning) for w in caught)


@pytest.mark.asyncio
async def test_a_graph_without_a_subgraph_is_unaffected() -> None:
    # The control for the probe above: the detection must key on the node being a compiled
    # graph, not on anything a normal node also has.
    context, recorder = make_context()
    graph = agentdx.instrument(build_pipeline(), name="pipeline", context=context)
    await graph.ainvoke({"task": "t"})
    assert [gap.construct for gap in graph.gaps] == []
    assert recorder.payloads(EventType.INSTRUMENTATION_GAP) == []
