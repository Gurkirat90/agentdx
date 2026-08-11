"""`@agentdx.agent` and `@agentdx.tool`: the events they emit, and what they never do."""

from __future__ import annotations

import asyncio

import pytest

import agentdx
from agentdx.events.schema import EventType
from agentdx.events.validators import validate_log
from agentdx.sdk.generic import MISSING_VALUE_HASH, use_run
from tests.unit.sdk.fakes import make_context


@pytest.mark.asyncio
async def test_an_agent_emits_a_well_formed_agent_step_span() -> None:
    context, recorder = make_context()

    @agentdx.agent("planner", role="orchestrator")
    async def planner(task: str) -> str:
        return f"plan for {task}"

    with use_run(context):
        assert await planner("ship it") == "plan for ship it"

    starts = recorder.of_type(EventType.SPAN_START)
    ends = recorder.of_type(EventType.SPAN_END)
    assert len(starts) == len(ends) == 1
    assert starts[0].payload["kind"] == "agent_step"
    assert starts[0].payload["attributes"] == {"role": "orchestrator"}
    assert starts[0].agent_id == "planner"
    assert ends[0].payload["status"] == "ok"
    assert ends[0].payload["error_type"] is None
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_sync_agent_produces_the_same_events_as_an_async_one() -> None:
    async_context, async_recorder = make_context()
    sync_context, sync_recorder = make_context()

    @agentdx.agent("worker")
    async def as_coroutine() -> int:
        return 1

    @agentdx.agent("worker")
    def as_function() -> int:
        return 1

    with use_run(async_context):
        await as_coroutine()
    with use_run(sync_context):
        as_function()

    def shape(events: list[object]) -> list[tuple[str, object]]:
        return [
            (event.type.value, event.payload.get("kind") or event.payload.get("status"))
            for event in events  # type: ignore[attr-defined]
        ]

    assert shape(async_recorder.events) == shape(sync_recorder.events)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_tool_records_arg_and_result_hashes_but_no_bodies_by_default() -> None:
    context, recorder = make_context(capture_bodies=False)

    @agentdx.tool("vector_search")
    async def vector_search(query: str, k: int = 5) -> list[str]:
        return [f"{query}-{index}" for index in range(k)]

    @agentdx.agent("researcher")
    async def researcher() -> list[str]:
        return await vector_search("secret-corpus-query", k=2)

    with use_run(context):
        await researcher()

    calls = recorder.payloads(EventType.TOOL_CALL)
    assert len(calls) == 1
    assert calls[0]["tool"] == "vector_search"
    assert calls[0]["status"] == "ok"
    assert str(calls[0]["args_hash"]).startswith("blake2b:")
    assert str(calls[0]["result_hash"]).startswith("blake2b:")
    assert "args" not in calls[0]
    assert "result" not in calls[0]
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_identical_tool_arguments_hash_identically() -> None:
    # PRD §16.3 / CONTEXT.md §3: redundancy detection is an exact hash of
    # (tool_name ‖ canonical_json(args)). Two identical calls must be indistinguishable.
    context, recorder = make_context()

    @agentdx.tool("fetch")
    async def fetch(url: str) -> str:
        return "body"

    @agentdx.agent("crawler")
    async def crawler() -> None:
        await fetch("https://example.com")
        await fetch("https://example.com")
        await fetch("https://example.org")

    with use_run(context):
        await crawler()

    hashes = [payload["args_hash"] for payload in recorder.payloads(EventType.TOOL_CALL)]
    assert hashes[0] == hashes[1]
    assert hashes[0] != hashes[2]


@pytest.mark.asyncio
async def test_a_failing_tool_is_recorded_and_the_exception_propagates() -> None:
    context, recorder = make_context()

    class Boom(RuntimeError):
        pass

    @agentdx.tool("flaky")
    async def flaky() -> None:
        detail = "upstream said no"
        raise Boom(detail)

    @agentdx.agent("caller")
    async def caller() -> None:
        await flaky()

    with use_run(context), pytest.raises(Boom, match="upstream said no"):
        await caller()

    call = recorder.payloads(EventType.TOOL_CALL)[0]
    assert call["status"] == "error"
    assert call["result_hash"] == MISSING_VALUE_HASH
    ends = recorder.of_type(EventType.SPAN_END)
    assert [end.payload["status"] for end in ends] == ["error", "error"]
    assert ends[0].payload["error_type"] == "Boom"
    assert ends[0].payload["error_message"] == "upstream said no"


@pytest.mark.asyncio
async def test_a_cancelled_agent_is_recorded_as_cancelled() -> None:
    context, recorder = make_context()
    entered = asyncio.Event()

    @agentdx.agent("slow")
    async def slow() -> None:
        entered.set()
        await asyncio.Event().wait()

    with use_run(context):
        task = asyncio.create_task(slow())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert recorder.of_type(EventType.SPAN_END)[0].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_float_attribute_is_a_hard_error_not_a_coercion() -> None:
    # ADR-007 consequence 3: "A user attaching a float to @agentdx.span(attributes=…) gets a
    # hard error, not a silent coercion — P04 must say so in the SDK docs."
    context, _ = make_context()

    @agentdx.agent("planner", attributes={"budget_ratio": 0.75})
    async def planner() -> None:
        return None

    with use_run(context), pytest.raises(agentdx.AttributeTypeError) as caught:
        await planner()
    assert "E-INSTR-005" in str(caught.value)
    assert "ADR-007" in str(caught.value)


@pytest.mark.asyncio
async def test_an_error_message_is_redacted_and_truncated() -> None:
    context, recorder = make_context()

    @agentdx.agent("leaky")
    async def leaky() -> None:
        detail = "auth failed for sk-" + "A" * 30 + " " + "x" * 900
        raise RuntimeError(detail)

    with use_run(context), pytest.raises(RuntimeError):
        await leaky()

    message = str(recorder.of_type(EventType.SPAN_END)[0].payload["error_message"])
    assert "sk-AAAA" not in message
    assert "[REDACTED]" in message
    assert len(message) <= 512


@pytest.mark.asyncio
async def test_a_decorated_function_keeps_its_identity() -> None:
    @agentdx.agent("keeper")
    async def documented(value: int) -> int:
        """A docstring the wrapper must not eat."""
        return value

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "A docstring the wrapper must not eat."


@pytest.mark.asyncio
async def test_calling_a_decorated_function_outside_a_run_names_the_fix() -> None:
    @agentdx.agent("stray")
    async def stray() -> None:
        return None

    with pytest.raises(agentdx.RunContextError) as caught:
        await stray()
    assert "E-INSTR-003" in str(caught.value)
    assert "agentdx.run" in str(caught.value)
