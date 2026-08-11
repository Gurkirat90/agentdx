"""Design constraint 3: context survives task boundaries and thread handoffs, or it errors.

"Wrong agent attribution corrupts every downstream analysis." That is the whole reason this
file exists, and it is why the last two tests matter as much as the first three: `contextvars`
propagate into `asyncio` tasks and into `asyncio.to_thread`, but **not** into a bare
`threading.Thread`. The SDK cannot make the third case work; what it can do is refuse to
guess, which is `E-INSTR-004`.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import agentdx
from agentdx.events.schema import EventType
from agentdx.sdk.generic import AgentContextError, active_agent, use_run
from tests.unit.sdk.fakes import make_context


@pytest.mark.asyncio
async def test_context_is_inherited_by_an_asyncio_task() -> None:
    context, recorder = make_context()

    @agentdx.tool("child")
    async def child(value: int) -> int:
        return value * 2

    @agentdx.agent("coder")
    async def coder() -> list[int]:
        # asyncio.Task copies the current context at creation time.
        return list(await asyncio.gather(child(1), child(2), child(3)))

    with use_run(context):
        assert await coder() == [2, 4, 6]

    tools = recorder.payloads(EventType.TOOL_CALL)
    assert len(tools) == 3
    agents = {event.agent_id for event in recorder.of_type(EventType.TOOL_CALL)}
    assert agents == {"coder"}


@pytest.mark.asyncio
async def test_context_is_inherited_across_asyncio_to_thread() -> None:
    context, recorder = make_context()

    @agentdx.tool("blocking_search")
    def blocking_search(query: str) -> str:
        # Runs on a worker thread. asyncio.to_thread copies the context for us.
        assert threading.current_thread() is not threading.main_thread()
        return f"result for {query}"

    @agentdx.agent("researcher")
    async def researcher() -> str:
        return str(await asyncio.to_thread(blocking_search, "langgraph"))

    with use_run(context):
        assert await researcher() == "result for langgraph"

    calls = recorder.of_type(EventType.TOOL_CALL)
    assert [event.agent_id for event in calls] == ["researcher"]
    assert calls[0].payload["tool"] == "blocking_search"


@pytest.mark.asyncio
async def test_context_is_inherited_by_an_explicitly_copied_executor_job() -> None:
    context, recorder = make_context()

    @agentdx.tool("cpu_bound")
    def cpu_bound(value: int) -> int:
        return value + 1

    @agentdx.agent("worker")
    async def worker() -> int:
        loop = asyncio.get_running_loop()
        carried = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return int(await loop.run_in_executor(pool, carried.run, cpu_bound, 41))

    with use_run(context):
        assert await worker() == 42

    assert [event.agent_id for event in recorder.of_type(EventType.TOOL_CALL)] == ["worker"]


@pytest.mark.asyncio
async def test_a_bare_thread_loses_the_context_and_the_sdk_refuses_to_guess() -> None:
    context, recorder = make_context()
    failures: list[BaseException] = []

    @agentdx.tool("orphan")
    def orphan() -> str:
        return "no context here"

    @agentdx.agent("planner")
    async def planner() -> None:
        def target() -> None:
            try:
                orphan()
            except BaseException as exc:  # noqa: BLE001 - the assertion is the exception type
                failures.append(exc)

        thread = threading.Thread(target=target)
        thread.start()
        await asyncio.to_thread(thread.join)

    with use_run(context):
        await planner()

    assert len(failures) == 1
    assert isinstance(failures[0], agentdx.RunContextError | AgentContextError)
    # And crucially: no tool_call was attributed to `planner`.
    assert recorder.of_type(EventType.TOOL_CALL) == []


@pytest.mark.asyncio
async def test_concurrent_scopes_of_one_agent_get_derived_clock_slots() -> None:
    # PRD §8.8: "Concurrent sub-tasks within one agent receive derived clock slots
    # (coder#1, coder#2) so that an agent racing itself is detectable (§14.2)."
    context, recorder = make_context()
    holder_open = asyncio.Event()
    release = asyncio.Event()

    @agentdx.agent("coder")
    async def holder() -> None:
        holder_open.set()
        await release.wait()

    @agentdx.agent("coder")
    async def overlapping() -> None:
        return None

    with use_run(context):
        held = asyncio.create_task(holder())
        await holder_open.wait()
        await overlapping()
        release.set()
        await held

    slots = sorted(
        {
            event.clock_slot
            for event in recorder.of_type(EventType.SPAN_START)
            if event.agent_id == "coder" and event.clock_slot is not None
        }
    )
    assert slots == ["coder", "coder#1"], slots


@pytest.mark.asyncio
async def test_the_agent_context_is_restored_after_a_scope_closes() -> None:
    context, _ = make_context()

    @agentdx.agent("outer")
    async def outer() -> str | None:
        inner_agent = active_agent()
        assert inner_agent is not None
        return inner_agent.agent_id

    with use_run(context):
        assert active_agent() is None
        assert await outer() == "outer"
        assert active_agent() is None
