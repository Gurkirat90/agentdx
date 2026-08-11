"""Generic-mode capture: explicit state, explicit messaging, hooks and `agentdx.run`."""

from __future__ import annotations

import asyncio

import pytest

import agentdx
from agentdx.events.schema import EventType
from agentdx.events.validators import validate_log
from agentdx.sdk.generic import (
    MISSING_VALUE_HASH,
    LifecycleHooks,
    RunResult,
    SpanRecord,
    install_runtime,
    use_run,
)
from tests.unit.sdk.fakes import FakeHost, make_context

# ---------------------------------------------------------------------------------------
# Explicit state (PRD §8.2 item 3)
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_reads_and_writes_carry_the_hashes_a_detector_needs() -> None:
    context, recorder = make_context()

    @agentdx.agent("coder")
    async def coder() -> object:
        async with agentdx.state() as shared:
            first = await shared.read("plan")
            await shared.write("plan", "v1")
            await shared.write("plan", "v2")
            return first

    with use_run(context):
        assert await coder() is None

    read = recorder.payloads(EventType.STATE_READ)[0]
    assert read["missing"] is True
    assert read["value_hash"] == MISSING_VALUE_HASH

    writes = recorder.payloads(EventType.STATE_WRITE)
    assert writes[0]["prev_value_hash"] is None
    assert writes[1]["prev_value_hash"] == writes[0]["value_hash"]
    assert writes[0]["value_hash"] != writes[1]["value_hash"]
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_present_null_is_distinguishable_from_an_absent_key() -> None:
    context, recorder = make_context()

    @agentdx.agent("coder")
    async def coder() -> None:
        async with agentdx.state() as shared:
            await shared.write("plan", None)
            await shared.read("plan")
            await shared.read("never_written")

    with use_run(context):
        await coder()

    reads = recorder.payloads(EventType.STATE_READ)
    assert reads[0]["missing"] is False
    assert reads[0]["value_hash"] != MISSING_VALUE_HASH
    assert reads[1]["missing"] is True


@pytest.mark.asyncio
async def test_no_body_is_written_by_default_and_one_is_under_opt_in() -> None:
    off, off_recorder = make_context(capture_bodies=False)
    on, on_recorder = make_context(capture_bodies=True)

    @agentdx.agent("coder")
    async def coder() -> None:
        async with agentdx.state() as shared:
            await shared.write("plan", "PLAINTEXT-SECRET")

    with use_run(off):
        await coder()
    with use_run(on):
        await coder()

    assert "value" not in off_recorder.payloads(EventType.STATE_WRITE)[0]
    assert "PLAINTEXT-SECRET" in str(on_recorder.payloads(EventType.STATE_WRITE)[0]["value"])


# ---------------------------------------------------------------------------------------
# Explicit messaging (PRD §8.4)
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_and_recv_create_the_only_happens_before_edge_between_agents() -> None:
    context, recorder = make_context()

    @agentdx.agent("coder")
    async def coder() -> None:
        await agentdx.send(to="reviewer", payload={"draft": "module_a"})

    @agentdx.agent("reviewer")
    async def reviewer() -> object:
        return await agentdx.recv()

    with use_run(context):
        received = (await asyncio.gather(coder(), reviewer()))[1]

    assert received == {"draft": "module_a"}
    send = recorder.of_type(EventType.MESSAGE_SEND)[0]
    recv = recorder.of_type(EventType.MESSAGE_RECV)[0]
    assert send.payload["to"] == "reviewer"
    assert send.payload["edge"] == "coder->reviewer"
    assert recv.payload["from"] == "coder"
    assert recv.payload["message_id"] == send.payload["message_id"]
    assert send.seq in recv.causal_parents
    assert recv.vclock["coder"] >= send.vclock["coder"], (
        "the receiver's clock must dominate the sender's, or PRD §14.2's partial order is "
        "not a partial order"
    )
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_message_payload_is_hashed_and_sized_but_not_inlined() -> None:
    context, recorder = make_context(capture_bodies=False)

    @agentdx.agent("coder")
    async def coder() -> None:
        await agentdx.send(to="reviewer", payload={"draft": "SENSITIVE"})

    with use_run(context):
        await coder()

    payload = recorder.payloads(EventType.MESSAGE_SEND)[0]
    assert str(payload["payload_hash"]).startswith("blake2b:")
    assert isinstance(payload["payload_bytes"], int)
    assert "SENSITIVE" not in str(payload)


# ---------------------------------------------------------------------------------------
# Lifecycle hooks (PRD §8.6)
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_hook_fires_and_receives_evidence_references() -> None:
    seen: list[str] = []
    spans: list[SpanRecord] = []
    results: list[RunResult] = []

    hooks = LifecycleHooks(
        on_run_start=lambda ctx: seen.append(f"run_start:{ctx.run_id}"),
        on_agent_start=lambda ctx, agent_id: seen.append(f"agent_start:{agent_id}"),
        on_span_end=lambda ctx, record: spans.append(record),
        on_run_end=lambda ctx, result: results.append(result),
    )
    context, _ = make_context(hooks=hooks)

    @agentdx.agent("coder")
    async def coder(payload: object) -> str:
        return "done"

    result = await agentdx.run(coder, task="ship it", host=FakeHost(context))

    assert seen == ["run_start:r_00abc", "agent_start:coder"]
    assert [record.kind for record in spans] == ["agent_step"]
    assert spans[0].start_seq == 0
    assert spans[0].end_seq == 1
    assert results == [result]
    assert result.status == "complete"


@pytest.mark.asyncio
async def test_a_hook_that_emits_an_event_is_refused() -> None:
    # PRD §8.6: hooks "must not perform I/O and must not mutate state — enforced by running
    # them under a guard that raises on any event emission or state write".
    def bad_hook(ctx: object, agent_id: str) -> None:
        from agentdx.sdk.generic import emit

        emit(ctx, EventType.INSTRUMENTATION_GAP, {"construct": "x", "location": "y", "reason": "z"})  # type: ignore[arg-type]

    context, _ = make_context(hooks=LifecycleHooks(on_agent_start=bad_hook))

    @agentdx.agent("coder")
    async def coder() -> None:
        return None

    with use_run(context), pytest.raises(agentdx.HookViolationError) as caught:
        await coder()
    assert "E-INSTR-007" in str(caught.value)


# ---------------------------------------------------------------------------------------
# agentdx.run (PRD §8.2 item 5)
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_without_a_runtime_names_the_prompt_that_supplies_one() -> None:
    @agentdx.agent("coder")
    async def coder(payload: object) -> None:
        return None

    with pytest.raises(agentdx.RunContextError) as caught:
        await agentdx.run(coder, task="t")
    assert "E-INSTR-003" in str(caught.value)
    assert "install_runtime" in str(caught.value)


@pytest.mark.asyncio
async def test_install_runtime_makes_the_one_argument_form_work() -> None:
    context, recorder = make_context()

    @agentdx.agent("coder")
    async def coder(payload: object) -> object:
        return payload

    previous = install_runtime(FakeHost(context))
    try:
        result = await agentdx.run(coder, task="ship it")
    finally:
        install_runtime(previous)

    assert result.output == {"task": "ship it"}
    assert recorder.of_type(EventType.SPAN_START)[0].agent_id == "coder"


@pytest.mark.asyncio
async def test_a_failing_run_is_still_sealed() -> None:
    context, _ = make_context()
    host = FakeHost(context)

    @agentdx.agent("coder")
    async def coder(payload: object) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await agentdx.run(coder, task="t", host=host)

    assert host.closed == ["failed"], "a crashed run is still a run and must be sealed"


@pytest.mark.asyncio
async def test_run_refuses_an_object_it_cannot_invoke() -> None:
    context, _ = make_context()
    with pytest.raises(agentdx.UnsupportedTargetError):
        await agentdx.run(object(), task="t", host=FakeHost(context))
