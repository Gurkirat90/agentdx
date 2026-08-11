"""The LangGraph adapter: the one-line path, and the thing that must fail loudly (PRD §8.3).

`agentdx.instrument(compiled_graph)` performs PRD §8.3's five bindings. **None of them
modifies a user node function**, and none of them monkey-patches a LangGraph internal.

| # | Binding | Mechanism here | Events |
|---|---|---|---|
| 1 | Node lifecycle | recording subclass of the node's `bound` class | `span_start`/`span_end` |
| 2 | Channel writes | recording subclass of each channel's own class | `state_write` |
| 3 | Channel reads | recording view over the node's input mapping | `state_read` |
| 4 | Edge traversal | the producer→consumer transition, at the consumer | `message_send/recv` |
| 5 | LLM / tool calls | the provider shim (§8.5) and `@agentdx.tool` | `llm_call`, `tool_call` |

**Why a subclass rather than a wrapper object.** PRD §8.3 asks for a "recording proxy", and
the obvious proxy — an object that forwards attribute access — breaks LangGraph, which does
`isinstance` checks on channels and runnables all through Pregel. A subclass of the object's
*own* class passes every one of those checks, survives the `copy()`/`from_checkpoint()`
round-trips Pregel performs between supersteps (both construct `self.__class__`), and needs
no knowledge of LangGraph's internals: this module imports **nothing** from `langgraph`. That
is deliberate. An adapter that imports a framework's private modules drifts on the framework's
schedule; one that duck-types drifts only when the shape it actually uses changes — and it
finds out at bind time, by probing, instead of at run time by silently recording less.

**The failure behaviour is the feature.** Every binding is *probed* before it is attached. A
probe that fails produces an `instrumentation_gap` event, an `InstrumentationGapWarning`, and
an entry in `RunResult.gaps`. When the failed binding is one without which the log would look
complete while being structurally incomplete — no node spans, or a **user channel whose
reducer is therefore unknown** — it also raises `InstrumentationError` (`E-INSTR-002`) and the
run does not start. PRD §8.3 calls reducer awareness "the single highest-risk false-positive
source in the product": a reduced channel is *designed* for concurrent writes, and without the
`reducer` mark the race detector reports a `lost_update` on almost every real LangGraph
application. Guessing is not available, and neither is continuing.

PRD §8.2 item 1 · §8.3 · §8.8 · §14.3, §14.7 · §36 (`E-INSTR-002`, `E-INSTR-006`).
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from agentdx.events.schema import EventType, PayloadValue
from agentdx.sdk.generic import (
    AgentContext,
    InstrumentationError,
    InstrumentationGap,
    LifecycleHooks,
    RunContext,
    Span,
    UnsupportedTargetError,
    active_run,
    agent_scope,
    current_agent,
    emit,
    hash_text,
    message_id_for,
    record_gap,
    state_facts,
    sync_agent_scope,
    use_run,
)

START_NODE: Final = "__start__"
"""LangGraph's synthetic entry node. It runs no user code, so it is not instrumented."""

END_NODE: Final = "__end__"
"""LangGraph's synthetic exit node. It is an edge target, never an agent."""

INTERNAL_CHANNEL_PREFIX: Final = "__"
"""Control-plane channels are named `__start__`, `__pregel_tasks`, `branch:to:<node>`.

They carry LangGraph's own scheduling, not user state, so a failure to instrument one is a
gap in edge derivation rather than in state capture — noted, but not fatal.
"""


class _ReducerFromInstance:
    """The table entry for a class whose reducer is a property of the *instance*.

    Guarantees: it is never a reducer name and never `None`. `None` in this table is a
    positive claim — "this class does not reduce" — and using it for a class that reduces
    through an instance attribute means an unreadable attribute degrades into that claim
    silently. This marker makes the two cases different values, so the failure has somewhere
    to go: `detect_reducer` returns `recognised=False` and the caller takes the same fatal
    `instrumentation_gap` path as a channel class nobody has ever heard of.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Return the constant's name, so a failed assertion reads as English."""
        return "REDUCER_FROM_INSTANCE"


REDUCER_FROM_INSTANCE: Final = _ReducerFromInstance()
"""Sentinel: this class is recognised, but its reducer must come from the instance."""

CHANNEL_REDUCERS: Final[Mapping[str, str | _ReducerFromInstance | None]] = {
    "LastValue": None,
    "LastValueAfterFinish": None,
    "EphemeralValue": None,
    "UntrackedValue": None,
    "AnyValue": None,
    "BinaryOperatorAggregate": REDUCER_FROM_INSTANCE,
    "DeltaChannel": REDUCER_FROM_INSTANCE,
    "Topic": "langgraph.channels.Topic",
    "NamedBarrierValue": "langgraph.channels.NamedBarrierValue",
    "NamedBarrierValueAfterFinish": "langgraph.channels.NamedBarrierValueAfterFinish",
}
"""The channel classes of the pinned LangGraph minor (ADR-003: `>=1.2,<1.3`), by class name.

`None` means "not a reducing channel": a write replaces the value, so two concurrent writes
are a genuine lost update. The two named entries reduce structurally rather than through a
callable. `REDUCER_FROM_INSTANCE` means the class *does* reduce and the callable lives on the
instance — which is how `Annotated[list, operator.add]` becomes `reducer="operator.add"` on
every `state_write` to that channel — so a class marked that way is **not** recognised when
the instance read fails.

**A class name absent from this table is an unrecognised channel type**, and so is a
`REDUCER_FROM_INSTANCE` class whose reducer cannot be read. For a user channel both are fatal,
by design: the alternative is recording `reducer=None` for a channel that does reduce, which
turns every concurrent write to it into a false `lost_update` (PRD §14.7). A LangGraph 1.3
bump lands here first, loudly, which is the point of keeping it a table.
"""

_REDUCER_ATTRIBUTES: Final = ("operator", "reducer")
"""Where a channel keeps its reducing callable, in the pinned minor's two reducing classes."""


# ---------------------------------------------------------------------------------------
# Reducer detection (PRD §8.3, "reducer awareness is mandatory")
# ---------------------------------------------------------------------------------------


def qualified_name(value: object) -> str:
    """Return a stable, human-readable name for a reducer callable.

    Guarantees: `operator.add` renders as `"operator.add"`, matching PRD §8.3's example,
    even though CPython reports its module as the private `_operator`. The result is stable
    across processes — it is derived from `__module__`/`__qualname__`, never from a repr,
    which for a `functools.partial` or a lambda would embed an address.
    """
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if name is None:
        return type(value).__name__
    if module is None or module == "builtins":
        return str(name)
    return f"{str(module).lstrip('_') if module == '_operator' else module}.{name}"


def detect_reducer(channel: object) -> tuple[str | None, bool]:
    """Return `(reducer_name, recognised)` for one channel.

    Guarantees: `recognised` is False for any channel class the pinned LangGraph minor did
    not have, **and** for a class whose table entry is `REDUCER_FROM_INSTANCE` but whose
    instance does not expose a callable reducer. `(None, True)` is therefore only ever
    returned for a class the table positively declares non-reducing; it is never the result
    of a read that failed. It is returned rather than raised so the caller can decide — an
    unrecognised *user* channel is fatal, an unrecognised control-plane channel is a noted
    gap.
    """
    class_name = type(channel).__name__
    if class_name not in CHANNEL_REDUCERS:
        return None, False
    for attribute in _REDUCER_ATTRIBUTES:
        reducer = getattr(channel, attribute, None)
        if callable(reducer):
            return qualified_name(reducer), True
    declared = CHANNEL_REDUCERS[class_name]
    if isinstance(declared, _ReducerFromInstance):
        return None, False
    return declared, True


def is_internal_channel(name: str) -> bool:
    """Return True for LangGraph's control-plane channels rather than user state."""
    return name.startswith(INTERNAL_CHANNEL_PREFIX) or ":" in name


# ---------------------------------------------------------------------------------------
# Binding probes — every attachment is checked before it is made
# ---------------------------------------------------------------------------------------


def accepts_input_and_config(method: object) -> bool:
    """Return True if a bound method can be called as `method(input, config)`.

    This is the probe that catches a changed callback signature — the exact drift PRD §8.3's
    proxy design exists to survive. It accepts `*args`, since a method that takes them can
    take anything; anything else must expose at least two callable-by-position parameters
    after `self`.
    """
    if not callable(method):
        return False
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 2


def _swap_class(instance: object, recording: type) -> object:
    """Return a copy of `instance` whose class is `recording`.

    Guarantees: the original object is untouched, so an `instrument()` that fails part-way
    leaves the user's graph exactly as it was.

    Raises:
        TypeError: the object's layout does not permit the swap — which is itself useful
            information, and the caller turns it into an `instrumentation_gap`.
    """
    duplicate = copy.copy(instance)
    duplicate.__class__ = recording
    return duplicate


# ---------------------------------------------------------------------------------------
# The recording state view (binding 3)
# ---------------------------------------------------------------------------------------


class RecordingStateView(Mapping[str, object]):
    """The mapping a node receives, recording each key the first time the node reads it.

    **It is a `collections.abc.Mapping`, not a `dict` subclass, and that is a correctness
    requirement rather than a taste.** CPython's `dict(x)` and `{**x}` take a C-level
    fast path when `x` is an actual `dict` subclass: they copy the underlying hash table
    directly and call neither `__getitem__` nor `keys()`. A recording view that subclassed
    `dict` therefore recorded **nothing** for the single most common way a real node reads
    its state — a capture hole with no `instrumentation_gap` beside it, which is the one
    failure mode this SDK exists to prevent (OP-2 finding D3). Deriving from the ABC removes
    the fast path: `dict(view)`, `{**view}`, `view.keys()`, `view.items()`, `view.values()`,
    `key in view` and iteration all route through `__getitem__`/`__iter__` and are recorded.

    The cost is that `isinstance(view, dict)` is now False. It is paid deliberately: the
    pinned LangGraph (ADR-003) passes a node's input straight to the node callable and does
    not require it to be a literal `dict`, and TypedDict-annotated nodes use mapping access
    only. A node that genuinely needs a `dict` gets one by writing `dict(state)` — which now
    also records the read it performs.

    A key is recorded once per node invocation (PRD §8.3 binding 3: "recorded as read the
    first time a node accesses it"). Reads after the node has returned are **not** recorded:
    they are not the node's reads, and attributing them to it would be a lie about causality.
    """

    def __init__(self, data: Mapping[str, object], on_read: Callable[[str, object], None]) -> None:
        """Wrap `data`, calling `on_read(key, value)` the first time each key is read."""
        self._data = dict(data)
        self._on_read = on_read
        self._seen: dict[str, None] = {}
        self._active = True

    def close(self) -> None:
        """Stop recording. Called when the node returns."""
        self._active = False

    def _note(self, key: str, value: object) -> None:
        """Record a first read of `key`, if the node is still running."""
        if not self._active or key in self._seen:
            return
        self._seen[key] = None
        self._on_read(key, value)

    def __getitem__(self, key: str) -> object:
        """Return the value and record the read."""
        value = self._data[key]
        self._note(key, value)
        return value

    def __iter__(self) -> Iterator[str]:
        """Iterate the keys, in the order the node's input presented them."""
        return iter(self._data)

    def __len__(self) -> int:
        """Return the number of keys, without recording a read of any of them."""
        return len(self._data)

    def __repr__(self) -> str:
        """Render like the mapping it stands in for, without recording reads."""
        return f"{type(self).__name__}({self._data!r})"

    def get(self, key: str, default: object = None) -> object:
        """Return the value or `default`, recording the read either way.

        Overridden rather than inherited because `Mapping.get` swallows the `KeyError` for a
        missing key and would therefore record nothing. A node asking for a key that is not
        there **is** a read: PRD §14.7's lost-update reasoning depends on knowing that a
        reader saw the key absent, which is why `state_read` carries `missing`.
        """
        try:
            value = self._data[key]
        except KeyError:
            value = default
        self._note(key, value)
        return value


# ---------------------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Completion:
    """What one finished node offers its consumers, for binding 4.

    `clock_slot` is carried rather than re-derived because the producer's scope has already
    exited by the time the consumer records the handoff — and re-deriving it from `agent_id`
    is what put a `message_send` on a different vector-clock slot from its own `span_start`.
    """

    agent_id: str
    clock_slot: str
    span_id: str
    payload_hash: str
    payload_bytes: int
    token: int


@dataclass(eq=False)
class LangGraphAdapter:
    """Holds one compiled graph's bindings and the per-run bookkeeping they need.

    Guarantees: `gaps` is the complete list of constructs that could not be bound, computed
    once at bind time and never quietly extended without an event being emitted for it.
    """

    name: str
    agent_from: Callable[[str], str]
    reducers: dict[str, str | None] = field(default_factory=dict)
    predecessors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Consumer → its producers. Binding 4 records the handoff **at the consumer** (PRD
    §8.3), so the forward `successors` map has no reader and is deliberately not kept: an
    unread index is a second source of truth waiting to disagree with the first."""
    gaps: list[InstrumentationGap] = field(default_factory=list)
    values: dict[str, object] = field(default_factory=dict)
    applied_hashes: dict[str, str] = field(default_factory=dict)
    declared_writes: dict[str, int] = field(default_factory=dict)
    applied_writes: dict[str, int] = field(default_factory=dict)
    input_writes: dict[str, int] = field(default_factory=dict)
    completed: dict[str, _Completion] = field(default_factory=dict)
    consumed: dict[str, int] = field(default_factory=dict)
    completion_token: int = 0
    run: RunContext | None = None

    # -- bind-time -----------------------------------------------------------------------

    def note_gap(self, construct: str, location: str, reason: str, *, fatal: bool) -> None:
        """Record a bind-time gap. It becomes an event when a run context exists."""
        self.gaps.append(
            InstrumentationGap(construct=construct, location=location, reason=reason, fatal=fatal)
        )

    # -- per-run -------------------------------------------------------------------------

    def reset(self, run: RunContext) -> None:
        """Bind the adapter to one run and clear the previous run's bookkeeping."""
        self.run = run
        self.values.clear()
        self.applied_hashes.clear()
        self.declared_writes.clear()
        self.applied_writes.clear()
        self.input_writes.clear()
        self.completed.clear()
        self.consumed.clear()
        self.completion_token = 0

    def require_run(self) -> RunContext:
        """Return the bound run context.

        Raises:
            RunContextError: the graph was invoked outside a run (`E-INSTR-003`).
        """
        if self.run is None:
            from agentdx.sdk.generic import RunContextError

            detail = (
                "an instrumented LangGraph node ran outside an AgentDX run. Invoke the "
                "object returned by `agentdx.instrument(...)`, not the original graph"
            )
            raise RunContextError(detail)
        return self.run

    # -- binding 2: channel writes -------------------------------------------------------

    def note_input(self, payload: object) -> None:
        """Record which channels the invocation's own input will write.

        Graph input arrives as a channel update like any other, but no node made it, so
        without this allowance the reconciliation below would report every input key as an
        uncaptured write on every run — a warning that is always wrong, which is the fastest
        way to teach people to ignore warnings.
        """
        if isinstance(payload, Mapping):
            for key in payload:
                self.input_writes[str(key)] = self.input_writes.get(str(key), 0) + 1

    def observe_channel_update(self, key: str, current: object) -> None:
        """Record that a channel actually changed, for the write-reconciliation check.

        The `state_write` event itself is emitted by the node that produced the update (see
        `record_writes`), because that is the only point at which the write can be
        attributed to an agent and to an open span — Pregel applies channel updates at the
        **end** of a superstep, outside every node's context, so a proxy that emitted there
        would attribute every write to nobody. This method does two things instead:

        * it keeps `applied_hashes` — the value as of the last completed superstep, which is
          what every node in the *current* superstep saw, and therefore the correct
          `prev_value_hash` for all of them. Two concurrent writers must agree on it, because
          that agreement is exactly what a lost update looks like;
        * it counts applied updates so that `reconcile` can notice a write nobody declared.
        """
        run = self.run
        self.values[key] = current
        if run is not None and not is_internal_channel(key):
            # Control-plane channels hold LangGraph's own sentinels, whose reprs embed a
            # memory address. Hashing one would emit a gap on literally every run, and a
            # warning that is always wrong is how people learn to ignore warnings.
            self.applied_hashes[key] = state_facts(run, key, current)[0]
        self.applied_writes[key] = self.applied_writes.get(key, 0) + 1

    def reconcile(self) -> None:
        """Emit a gap for any user channel whose applied writes nobody accounted for.

        Guarantees: this is the check that makes binding 2 more than decoration. If a node
        writes a channel by a route the adapter does not see — a `Command`, a `Send`, a
        future LangGraph write path — the counts disagree and the log says so, instead of
        the state history quietly missing a write.
        """
        run = self.run
        if run is None:
            return
        for key in sorted(self.applied_writes):
            if is_internal_channel(key):
                continue
            applied = self.applied_writes[key] - self.input_writes.get(key, 0)
            declared = self.declared_writes.get(key, 0)
            if applied > declared:
                record_gap(
                    run,
                    "state_write",
                    f"{self.name}.{key}",
                    f"{applied} channel update(s) were applied but {declared} were "
                    f"attributable to a node, so {applied - declared} state_write event(s) "
                    f"are missing from this log",
                )

    # -- binding 3: reads ----------------------------------------------------------------

    def record_read(self, key: str, value: object) -> None:
        """Emit `state_read` for a node's first access to a channel."""
        run = self.require_run()
        agent = current_agent()
        value_hash, text = state_facts(run, key, value)
        payload: dict[str, PayloadValue] = {
            "key": key,
            "value_hash": value_hash,
            "missing": value is None and key not in self.values,
        }
        if run.capture_bodies and text is not None:
            payload["value"] = run.redactor.scrub(text)
        emit(
            run,
            EventType.STATE_READ,
            payload,
            agent_id=agent.agent_id,
            clock_slot=agent.clock_slot,
            span_id=_span_id(agent),
        )

    # -- binding 2 (emission) + binding 4 (edges) ----------------------------------------

    def record_writes(self, node_name: str, agent_id: str, open_span: Span, result: object) -> None:
        """Emit `state_write` for everything a node returned, and record its completion.

        Guarantees: the writes are attributed to the node that produced them, inside its own
        span, at the moment it produced them — which is what makes the log usable for race
        detection at all. `prev_value_hash` is the value as of the start of this superstep,
        which is exactly what two concurrent writers both saw and therefore exactly what a
        lost update looks like.

        `clock_slot` is the ambient agent context's, never the bare `agent_id`: when
        `agent_from` maps two nodes onto one agent id they are concurrent within a superstep
        and hold *different* slots, and stamping the write with the agent id would move it
        onto a slot its own span never occupied.

        A node whose return value the adapter cannot interpret produces an
        `instrumentation_gap` rather than silence.
        """
        run = self.require_run()
        clock_slot = current_agent().clock_slot
        updates = self._updates_of(result)
        if updates is None:
            record_gap(
                run,
                "node_output",
                f"{self.name}.{node_name}",
                f"a node returned {type(result).__name__}, which the adapter cannot read as "
                f"a set of channel updates, so its state writes are not in this log",
            )
            updates = {}

        parts: list[str] = []
        for key in sorted(updates):
            value = updates[key]
            value_hash, text = state_facts(run, key, value)
            payload: dict[str, PayloadValue] = {
                "key": key,
                "value_hash": value_hash,
                "prev_value_hash": self.applied_hashes.get(key),
                "reducer": self.reducers.get(key),
                "txn_id": None,
                "lock_id": None,
            }
            if run.capture_bodies and text is not None:
                payload["value"] = run.redactor.scrub(text)
            emit(
                run,
                EventType.STATE_WRITE,
                payload,
                agent_id=agent_id,
                clock_slot=clock_slot,
                span_id=open_span.span_id,
                causes=(open_span.start_seq,),
            )
            self.declared_writes[key] = self.declared_writes.get(key, 0) + 1
            parts.append(f"{key}={value_hash}")

        delta = "{" + ",".join(parts) + "}"
        self.completion_token += 1
        self.completed[node_name] = _Completion(
            agent_id=agent_id,
            clock_slot=clock_slot,
            span_id=open_span.span_id,
            payload_hash=hash_text(delta),
            payload_bytes=len(delta.encode("utf-8")),
            token=self.completion_token,
        )

    def _updates_of(self, result: object) -> Mapping[str, object] | None:
        """Return the channel updates a node's return value represents, or None."""
        if result is None:
            return {}
        if isinstance(result, Mapping):
            return {str(k): v for k, v in result.items()}
        update = getattr(result, "update", None)
        if isinstance(update, Mapping):
            return {str(k): v for k, v in update.items()}
        return None

    def deliver_edges(self, node_name: str, agent_id: str, open_span: Span) -> None:
        """Emit the `message_send`/`message_recv` pair for every edge feeding this node.

        PRD §8.3 binding 4 makes the *transition* — not the node — the thing that records a
        message, and recording it at the consumer is what makes the pair exact: a
        conditional edge that was not taken produces no send, so every `message_send` in an
        AgentDX log has exactly one `message_recv` and a missing recv means a genuinely
        dropped message (PRD §6.1, §12.2 `message_drop`).

        The send is attributed to the producing node and its span; the recv to this node.
        The recv names the send as a causal parent, which is the only happens-before edge
        between two agents in the entire system (PRD §14.3).
        """
        run = self.require_run()
        agent = current_agent()
        for producer in self.predecessors.get(node_name, ()):
            completion = self.completed.get(producer)
            if completion is None:
                continue
            edge_key = f"{producer}->{node_name}"
            if self.consumed.get(edge_key, -1) >= completion.token:
                continue
            self.consumed[edge_key] = completion.token
            run.registry.message_seq += 1
            message_id = message_id_for(run.run_id, producer, node_name, run.registry.message_seq)
            send_seq = emit(
                run,
                EventType.MESSAGE_SEND,
                {
                    "message_id": message_id,
                    "to": node_name,
                    "edge": edge_key,
                    "payload_hash": completion.payload_hash,
                    "payload_bytes": completion.payload_bytes,
                },
                agent_id=completion.agent_id,
                clock_slot=completion.clock_slot,
                span_id=completion.span_id,
            )
            emit(
                run,
                EventType.MESSAGE_RECV,
                {
                    "message_id": message_id,
                    "from": producer,
                    "edge": edge_key,
                    "delivered_virtual_ts_ms": run.clock.virtual_ms(),
                    "reordered": False,
                    "duplicate": False,
                },
                agent_id=agent_id,
                clock_slot=agent.clock_slot,
                span_id=open_span.span_id,
                causes=(send_seq,),
            )

    # -- binding 1: the node wrappers ----------------------------------------------------

    def clock_slot_for(self, node_name: str, agent_id: str) -> str:
        """Return the vector-clock slot one node's events belong on (PRD §14.2).

        Guarantees: two different nodes never share a slot, and a node whose `agent_from`
        is the identity map keeps the bare agent id — so the default one-line integration
        produces exactly the slots it always did.

        `agent_from` exists so several nodes can be one agent (PRD §8.2 item 1), and nodes
        in one Pregel superstep are concurrent by that framework's semantics. Putting two
        such nodes on one slot makes the vector clock assert an ordering the run did not
        have, which hides every race between them. Where a real ordering exists it is
        carried by binding 4's `message_send`/`message_recv` pair, whose `causal_parents`
        merge the two clocks — so splitting the slot loses nothing that was true.

        The slot is derived from the node name, never from a counter or task identity, so it
        is identical on every replay (I1).
        """
        return agent_id if node_name == agent_id else f"{agent_id}#{node_name}"

    async def run_node_async(
        self,
        node_name: str,
        base: type,
        bound: object,
        node_input: object,
        config: object,
        kwargs: Mapping[str, object],
    ) -> object:
        """Run one node inside its agent span, recording reads, writes and edges."""
        agent_id = self.agent_from(node_name)
        async with agent_scope(
            agent_id, name=node_name, clock_slot=self.clock_slot_for(node_name, agent_id)
        ) as open_span:
            self.deliver_edges(node_name, agent_id, open_span)
            view = self._view(node_input)
            try:
                result = await base.ainvoke(bound, view, config, **kwargs)  # type: ignore[attr-defined]
            finally:
                if isinstance(view, RecordingStateView):
                    view.close()
            self.record_writes(node_name, agent_id, open_span, result)
            return result

    def run_node_sync(
        self,
        node_name: str,
        base: type,
        bound: object,
        node_input: object,
        config: object,
        kwargs: Mapping[str, object],
    ) -> object:
        """The synchronous form of `run_node_async`, for sync node callables."""
        agent_id = self.agent_from(node_name)
        with sync_agent_scope(
            agent_id, name=node_name, clock_slot=self.clock_slot_for(node_name, agent_id)
        ) as open_span:
            self.deliver_edges(node_name, agent_id, open_span)
            view = self._view(node_input)
            try:
                result = base.invoke(bound, view, config, **kwargs)  # type: ignore[attr-defined]
            finally:
                if isinstance(view, RecordingStateView):
                    view.close()
            self.record_writes(node_name, agent_id, open_span, result)
            return result

    def _view(self, node_input: object) -> object:
        """Wrap a node's input so first reads are recorded, or note why it cannot be."""
        if isinstance(node_input, Mapping):
            return RecordingStateView(node_input, self.record_read)
        run = self.run
        if run is not None:
            record_gap(
                run,
                "state_read",
                self.name,
                f"a node's input is a {type(node_input).__name__}, not a mapping, so the "
                f"keys it reads are not in this log",
            )
        return node_input


def _span_id(agent: AgentContext) -> str:
    """Return the innermost open span of an agent context.

    Raises:
        AgentContextError: no span is open (`E-INSTR-004`).
    """
    from agentdx.sdk.generic import AgentContextError

    if agent.span_id is None:
        detail = "an instrumented LangGraph node emitted an event with no open span"
        raise AgentContextError(detail)
    return agent.span_id


# ---------------------------------------------------------------------------------------
# The instrumented graph
# ---------------------------------------------------------------------------------------


class InstrumentedGraph:
    """What `agentdx.instrument()` returns: a drop-in stand-in for the compiled graph.

    Guarantees:

    * Every attribute the original graph exposed is still reachable, so an instrumented
      graph can be passed anywhere the original went — that is what makes the integration
      one line rather than one line plus a call-site change.
    * `ainvoke`/`invoke` bind a run context, emit any buffered `instrumentation_gap` events,
      reconcile the channel writes at the end, and return exactly what the graph returned.
    * The graph handed to `instrument()` is **not** modified: the bindings live on a copy
      whose node and channel registries are fresh dicts. When a graph cannot be copied the
      bindings go on in place, and that is recorded as an `instrumentation_gap` rather than
      done quietly.
    """

    def __init__(
        self,
        graph: object,
        adapter: LangGraphAdapter,
        *,
        context: RunContext | None = None,
        hooks: LifecycleHooks | None = None,
    ) -> None:
        """Bind an instrumented graph to its adapter and, optionally, to a run context."""
        self._graph = graph
        self._adapter = adapter
        self._context = context
        self._hooks = hooks
        self._gaps_emitted = False

    @property
    def adapter(self) -> LangGraphAdapter:
        """Return the adapter, so a caller can inspect the bindings and the gaps."""
        return self._adapter

    @property
    def graph(self) -> object:
        """Return the underlying compiled graph, with the recording bindings attached."""
        return self._graph

    @property
    def gaps(self) -> tuple[InstrumentationGap, ...]:
        """Return every construct this adapter could not bind."""
        return tuple(self._adapter.gaps)

    def __getattr__(self, item: str) -> object:
        """Delegate everything the adapter does not override to the compiled graph."""
        return getattr(self._graph, item)

    def _resolve(self) -> RunContext:
        """Return the run context this invocation records into.

        Raises:
            RunContextError: neither an explicit context nor an ambient run exists
                (`E-INSTR-003`).
        """
        if self._context is not None:
            return self._context
        from agentdx.sdk.generic import current_run

        return current_run()

    def emit_pending_gaps(self, run: RunContext) -> None:
        """Write every gap found at bind time into the log, exactly once.

        Bind time is before any run exists, so a gap discovered then has nowhere to be
        recorded yet. Buffering it and emitting it at the first invocation is what keeps the
        promise that a capture hole is always *in the log*, not only in a warning that has
        already scrolled past.
        """
        if self._gaps_emitted:
            return
        self._gaps_emitted = True
        with use_run(run):
            for gap in self._adapter.gaps:
                record_gap(run, gap.construct, gap.location, gap.reason, fatal=gap.fatal)

    def _open(self, run: RunContext, node_input: object) -> None:
        """Prepare one invocation: bind the run, emit buffered gaps, stop if any is fatal."""
        self._adapter.reset(run)
        self._adapter.note_input(node_input)
        self.emit_pending_gaps(run)
        fatal = [gap for gap in self._adapter.gaps if gap.fatal]
        if fatal:
            raise InstrumentationError(_fatal_message(self._adapter.name, fatal))

    async def ainvoke(self, node_input: object, config: object = None, **kwargs: object) -> object:
        """Invoke the graph, recording it. Returns exactly what the graph returned."""
        run = self._resolve()
        with use_run(run):
            self._open(run, node_input)
            try:
                return await self._graph.ainvoke(node_input, config, **kwargs)  # type: ignore[attr-defined]
            finally:
                self._adapter.reconcile()

    def invoke(self, node_input: object, config: object = None, **kwargs: object) -> object:
        """The synchronous form of `ainvoke`."""
        run = self._resolve()
        with use_run(run):
            self._open(run, node_input)
            try:
                return self._graph.invoke(node_input, config, **kwargs)  # type: ignore[attr-defined]
            finally:
                self._adapter.reconcile()


def _fatal_message(name: str, gaps: Sequence[InstrumentationGap]) -> str:
    """Render the message of a fatal instrumentation failure, naming every construct."""
    listed = "; ".join(f"{gap.construct} at {gap.location}: {gap.reason}" for gap in gaps)
    return (
        f"the LangGraph adapter could not attach to {len(gaps)} required construct(s) in "
        f"{name!r} and will not record a partially-captured run — analysis over a silently "
        f"incomplete log produces confident wrong answers. {listed}. Either pin LangGraph to "
        f"the supported range (ADR-003: >=1.2,<1.3) or use the decorator API "
        f"(`@agentdx.agent`) for the constructs the adapter cannot see"
    )


# ---------------------------------------------------------------------------------------
# The one-line entry point
# ---------------------------------------------------------------------------------------


def _identity(node_name: str) -> str:
    """Return the node name unchanged — the default `agent_from` (PRD §8.2 item 1)."""
    return node_name


def _mapping_attribute(target: object, attribute: str) -> Mapping[str, object] | None:
    """Return a mapping attribute of the target, or None if it is absent or not a mapping."""
    value = getattr(target, attribute, None)
    return value if isinstance(value, Mapping) else None


def is_compiled_graph(target: object) -> bool:
    """Return True if `target` has the shape of a compiled LangGraph graph.

    Guarantees: the same three-part probe `bind` uses to accept an instrumentation target —
    `.nodes`, `.channels` and `.ainvoke` — so "a thing this adapter would instrument" and
    "a thing that must not be treated as one opaque node" are decided by one predicate and
    cannot drift apart. Duck-typed, because this module imports nothing from `langgraph`.
    """
    return (
        _mapping_attribute(target, "nodes") is not None
        and _mapping_attribute(target, "channels") is not None
        and callable(getattr(target, "ainvoke", None))
    )


def _edges_of(target: object) -> Iterator[tuple[str, str]]:
    """Yield the static `(source, target)` edges of a compiled graph, in sorted order.

    Sorted because `StateGraph.edges` is a `set`, and set iteration order is not a contract
    (AGENTS.md §4.1). An unsorted walk here would make the adapter's own bookkeeping — and
    therefore the order of `message_send` events — differ between processes.
    """
    builder = getattr(target, "builder", None)
    edges = getattr(builder, "edges", None)
    if edges is None:
        return
    pairs: list[tuple[str, str]] = []
    for edge in edges:
        if isinstance(edge, tuple) and len(edge) == 2:
            pairs.append((str(edge[0]), str(edge[1])))
    yield from sorted(pairs)


def bind(
    target: object,
    *,
    name: str,
    agent_from: Callable[[str], str] = _identity,
) -> tuple[LangGraphAdapter, object]:
    """Attach the five PRD §8.3 bindings, probing each one first.

    Guarantees: **the caller's graph is not modified.** The bindings are attached to a copy
    whose node and channel registries are fresh dicts, so the object the user compiled keeps
    working exactly as it did — instrumentation is not a one-way door. If the graph exposes
    no `copy()` (which the pinned LangGraph does), the registries are rebound in place and
    that fact is recorded as a gap rather than done quietly.

    Returns:
        The adapter and the graph the bindings were attached to.

    Raises:
        UnsupportedTargetError: the object is not a compiled graph shape this adapter knows
            (`E-INSTR-006`).
    """
    if not is_compiled_graph(target):
        detail = (
            f"{type(target).__name__} is not a LangGraph compiled graph: "
            f"`agentdx.instrument()` needs `.nodes`, `.channels` and `.ainvoke`. Use the "
            f"decorator API (`@agentdx.agent`, `@agentdx.tool`) for plain-Python systems"
        )
        raise UnsupportedTargetError(detail)

    nodes = _mapping_attribute(target, "nodes") or {}
    channels = _mapping_attribute(target, "channels") or {}
    adapter = LangGraphAdapter(name=name, agent_from=agent_from)
    working = _detached_copy(target, nodes, channels, adapter)
    working_nodes = _mapping_attribute(working, "nodes") or nodes
    working_channels = _mapping_attribute(working, "channels") or channels
    _bind_nodes(working_nodes, adapter)
    _bind_channels(working_channels, adapter)
    _bind_edges(working, adapter)
    return adapter, working


def _detached_copy(
    target: object,
    nodes: Mapping[str, object],
    channels: Mapping[str, object],
    adapter: LangGraphAdapter,
) -> object:
    """Return a copy of the graph with fresh registries, or the target itself with a gap.

    Guarantees: never raises. A graph that cannot be copied is still instrumented — in
    place — and the log says so, because "we edited your object" is a fact a user is
    entitled to and a `TypeError` here would be a worse outcome than a warning.
    """
    copier = getattr(target, "copy", None)
    if callable(copier):
        try:
            duplicate = copier({"nodes": dict(nodes), "channels": dict(channels)})
        except (TypeError, ValueError, AttributeError) as exc:
            adapter.note_gap(
                "graph_copy",
                adapter.name,
                f"the compiled graph could not be copied ({exc}), so the object passed to "
                f"instrument() has been instrumented in place",
                fatal=False,
            )
            return target
        return duplicate
    adapter.note_gap(
        "graph_copy",
        adapter.name,
        "the compiled graph exposes no copy(), so the object passed to instrument() has "
        "been instrumented in place",
        fatal=False,
    )
    return target


def _bind_nodes(nodes: Mapping[str, object], adapter: LangGraphAdapter) -> None:
    """Binding 1: wrap every user node's callable in a recording subclass of its own class."""
    replacements: dict[str, object] = {}
    for node_name in sorted(nodes):
        if node_name == START_NODE:
            continue
        node = nodes[node_name]
        bound = getattr(node, "bound", None)
        copier = getattr(node, "copy", None)
        if is_compiled_graph(bound):
            adapter.note_gap(
                "subgraph",
                f"{name_of(adapter)}.{node_name}",
                "the node is a compiled subgraph, whose own nodes this adapter does not "
                "walk: their spans, state accesses and handoffs would be absent from the "
                "log while the subgraph appeared as one opaque agent that did all of it. "
                "Recursive path-qualified subgraph capture is not implemented; instrument "
                "the subgraph separately, or inline its nodes into the parent graph",
                fatal=True,
            )
            continue
        if bound is None or not callable(copier):
            adapter.note_gap(
                "node_lifecycle",
                f"{name_of(adapter)}.{node_name}",
                "the node exposes no `bound` callable or no `copy()`, so its span cannot be "
                "recorded; the adapter is pinned to LangGraph >=1.2,<1.3 (ADR-003)",
                fatal=True,
            )
            continue
        base = type(bound)
        if not accepts_input_and_config(base.ainvoke) or not accepts_input_and_config(base.invoke):
            adapter.note_gap(
                "node_callback_signature",
                f"{name_of(adapter)}.{node_name}",
                f"{base.__name__}.invoke/ainvoke no longer accept (input, config); the "
                f"callback signature changed under the pinned LangGraph range (ADR-003), so "
                f"node spans cannot be recorded",
                fatal=True,
            )
            continue
        try:
            recording = _swap_class(bound, _recording_bound_class(base, adapter, node_name))
        except TypeError as exc:
            adapter.note_gap(
                "node_lifecycle",
                f"{name_of(adapter)}.{node_name}",
                f"the node's callable could not be wrapped ({exc})",
                fatal=True,
            )
            continue
        replacements[node_name] = copier({"bound": recording})

    for node_name, replacement in replacements.items():
        _assign(nodes, node_name, replacement)


def _bind_channels(channels: Mapping[str, object], adapter: LangGraphAdapter) -> None:
    """Bindings 2 and 3: wrap every channel and record its reducer."""
    replacements: dict[str, object] = {}
    for channel_name in sorted(channels):
        channel = channels[channel_name]
        internal = is_internal_channel(channel_name)
        reducer, recognised = detect_reducer(channel)
        if not recognised:
            adapter.note_gap(
                "channel_type",
                f"{name_of(adapter)}.{channel_name}",
                f"{type(channel).__name__} is not a channel type this adapter recognises, so "
                f"its reducer is unknown. A reduced channel is designed for concurrent "
                f"writes; recording `reducer=null` for one would make the race detector "
                f"report a lost update on every concurrent write to it (PRD §8.3, §14.7)",
                fatal=not internal,
            )
            continue
        adapter.reducers[channel_name] = reducer
        try:
            replacements[channel_name] = _swap_class(
                channel, _recording_channel_class(type(channel), adapter, channel_name)
            )
        except TypeError as exc:
            adapter.note_gap(
                "channel_write",
                f"{name_of(adapter)}.{channel_name}",
                f"the channel could not be wrapped ({exc}), so writes to it are not "
                f"reconciled against the nodes that made them",
                fatal=False,
            )

    for channel_name, replacement in replacements.items():
        _assign(channels, channel_name, replacement)


def _bind_edges(target: object, adapter: LangGraphAdapter) -> None:
    """Binding 4: derive the static edge structure that message events are recorded against."""
    predecessors: dict[str, list[str]] = {}
    found = False
    for source, destination in _edges_of(target):
        found = True
        if source in (START_NODE, END_NODE) or destination in (START_NODE, END_NODE):
            continue
        predecessors.setdefault(destination, []).append(source)

    if not found:
        adapter.note_gap(
            "edge_traversal",
            name_of(adapter),
            "the graph exposes no static edge set, so producer→consumer handoffs are not "
            "recorded as messages. Messages are the only carrier of happens-before between "
            "agents (PRD §14.3), so causality between nodes will be missing from this log",
            fatal=False,
        )
    adapter.predecessors = {k: tuple(sorted(v)) for k, v in sorted(predecessors.items())}


def name_of(adapter: LangGraphAdapter) -> str:
    """Return the instrumented graph's name, for gap locations."""
    return adapter.name


def _assign(mapping: Mapping[str, object], key: str, value: object) -> None:
    """Write into a mapping that is declared read-only but is a `dict` at run time.

    Raises:
        UnsupportedTargetError: the registry does not support assignment (`E-INSTR-006`).
    """
    setter = getattr(mapping, "__setitem__", None)
    if setter is None:
        detail = (
            f"the graph's registry for {key!r} is immutable, so the recording bindings "
            f"cannot be attached"
        )
        raise UnsupportedTargetError(detail)
    setter(key, value)


def _recording_bound_class(base: type, adapter: LangGraphAdapter, node_name: str) -> type:
    """Return a subclass of a node callable's class that records the node's span."""

    def invoke(self: object, node_input: object, config: object = None, **kwargs: object) -> object:
        """Run the node synchronously, inside its span."""
        return adapter.run_node_sync(node_name, base, self, node_input, config, kwargs)

    async def ainvoke(
        self: object, node_input: object, config: object = None, **kwargs: object
    ) -> object:
        """Run the node asynchronously, inside its span."""
        return await adapter.run_node_async(node_name, base, self, node_input, config, kwargs)

    return type(
        f"AgentDXRecording{base.__name__}",
        (base,),
        {"invoke": invoke, "ainvoke": ainvoke, "__slots__": ()},
    )


def _recording_channel_class(base: type, adapter: LangGraphAdapter, key: str) -> type:
    """Return a subclass of a channel's class that observes every applied update.

    `__slots__ = ()` is required, not cosmetic: LangGraph channels declare `__slots__`, and a
    subclass that added a `__dict__` would change the object layout and make the class swap
    impossible. Because the subclass adds no state, `copy()` and `from_checkpoint()` — both of
    which construct `self.__class__` — keep the recording behaviour across supersteps.
    """

    def update(self: object, values: Sequence[object]) -> object:
        """Apply the update, then record that the channel changed."""
        changed = base.update(self, values)  # type: ignore[attr-defined]
        if changed:
            adapter.observe_channel_update(key, _checkpoint_of(self, base))
        return changed

    return type(f"AgentDXRecording{base.__name__}", (base,), {"update": update, "__slots__": ()})


def _checkpoint_of(channel: object, base: type) -> object:
    """Return a channel's current value, or None when it is empty.

    Guarantees: never raises. LangGraph signals an empty channel with `EmptyChannelError`,
    which this adapter cannot name without importing LangGraph — so any exception from the
    read is treated as "empty", which is the only thing it can mean here.
    """
    checkpoint = getattr(base, "checkpoint", None)
    if checkpoint is None:
        return None
    try:
        return checkpoint(channel)
    except Exception:  # noqa: BLE001 - an empty channel raises a LangGraph-private type
        return None


def instrument(
    target: object,
    *,
    name: str,
    capture_bodies: bool | None = None,
    agent_from: Callable[[str], str] = _identity,
    context: RunContext | None = None,
    hooks: LifecycleHooks | None = None,
) -> InstrumentedGraph:
    """Instrument a compiled LangGraph graph in one line (PRD §8.2 item 1).

    Guarantees:

    * **Zero changes to prompt or agent logic** (PRD §8.2's design constraint). Node
      functions are not touched; the adapter attaches to the compiled graph's registries.
    * Every binding is probed before it is attached, and every failed probe becomes an
      `instrumentation_gap` event plus a warning. A failure that would leave the log
      structurally incomplete raises `InstrumentationError` instead of degrading quietly.
    * `capture_bodies` defaults to `[privacy] capture_bodies`, which defaults to False
      (invariant I8). It is accepted here because PRD §8.2 shows it here.

    Args:
        target: A LangGraph `CompiledStateGraph`.
        name: The graph's name, used in gap locations and in the run summary.
        capture_bodies: PRD §8.11 opt-in. Applied to the explicit `context` if one is given.
        agent_from: Maps a node name to an agent id (PRD §8.2 item 1). The identity map by
            default; identity must be stable across runs or PRD §17's baseline breaks.
        context: The run this graph records into. When omitted, the ambient run at invoke
            time is used — which is what `agentdx.run()` establishes.
        hooks: PRD §8.6 lifecycle hooks.

    Raises:
        UnsupportedTargetError: the object is not a compiled graph (`E-INSTR-006`).
        InstrumentationError: a required binding could not be made (`E-INSTR-002`).
    """
    adapter, working = bind(target, name=name, agent_from=agent_from)
    resolved = context
    if resolved is not None and capture_bodies is not None:
        resolved = _with_bodies(resolved, capture_bodies=capture_bodies)
    graph = InstrumentedGraph(working, adapter, context=resolved, hooks=hooks)

    run = resolved if resolved is not None else active_run()
    if run is not None:
        # A run already exists, so a gap found at bind time can be written to the log now
        # rather than at the first invocation — which for a fatal gap is the difference
        # between the log naming the failure and the log not existing.
        graph.emit_pending_gaps(run)
    fatal = [gap for gap in adapter.gaps if gap.fatal]
    if fatal:
        raise InstrumentationError(_fatal_message(name, fatal))
    return graph


def _with_bodies(context: RunContext, *, capture_bodies: bool) -> RunContext:
    """Return the context with `capture_bodies` overridden, sharing the same registry."""
    from dataclasses import replace

    return replace(context, capture_bodies=capture_bodies)


__all__ = [
    "CHANNEL_REDUCERS",
    "END_NODE",
    "INTERNAL_CHANNEL_PREFIX",
    "REDUCER_FROM_INSTANCE",
    "START_NODE",
    "InstrumentedGraph",
    "LangGraphAdapter",
    "RecordingStateView",
    "accepts_input_and_config",
    "bind",
    "detect_reducer",
    "instrument",
    "is_compiled_graph",
    "is_internal_channel",
    "qualified_name",
]
