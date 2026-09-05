"""OP-2 second-pass finding #2 (`op2-audit-p06-second.md`): ADR-021's fix, generalized.

ADR-021 (`tests/integration/runtime/test_root_entry_drain.py`) protects *root's* first
dispatch: `agentdx.run()` calls `context.scheduler.yield_point("sdk_run_entry")` once,
unconditionally, before LangGraph's own multi-tick Pregel/`ainvoke()` entry machinery is ever
reached — because `Scheduler._resume_task` gives a task's first dispatch exactly one real
event-loop tick, and root's raw entry into that machinery does not reliably fit in one tick on
real hardware.

The audit found that fix was wired into `sdk/generic.py::run()` only — nothing equivalent
existed for a *spawned* node body's own first dispatch, so a node whose own body needs several
real ticks to settle before its own first genuine scheduler-visible call (the same shape
`test_root_entry_drain.py` uses for root, one level down — e.g. a subgraph node whose first
superstep immediately fans out) reproduces the identical deadlock, one level down, in code no
prior audit or test exercised (`op2-audit-p06-second.md`'s `repro5_nested_immediate_fanout.py`
demonstrated this directly against the raw `Scheduler`, and 9-plus of a 180-run fuzz sweep's 55
failures showed the same signature — a `join(...)` wait-reason on the parent, paired with an
*empty* wait-reason on the child it's joining).

The repair reuses ADR-021's own pattern verbatim at the new call site:
`sdk/langgraph.py::LangGraphAdapter._run_node_body` now takes the same `yield_point` (reason
`"sdk_node_entry"`) as the very first thing it does, before `use_run`/`agent_scope`/
`base.ainvoke` are ever reached — so this task's *next* suspension is a resumption (which
tolerates `resume_drain_ticks`, 200), not its raw first dispatch (one tick only).

Mirrors `test_root_entry_drain.py`'s own two-test structure exactly: the first test proves the
fix directly, through the real, shipped adapter (`agentdx.instrument`) and a real `Scheduler` —
not a scheduler-primitive stand-in — so it also incidentally confirms the fix is actually wired
into the call path real usage takes, not just present in source. The second test is the decisive
half: the identical shape, spawned directly on the scheduler without going through
`_run_node_body` at all (mirroring the audit's own `repro5`), still deadlocks — proving the
first test's "survives" result is not vacuously true of any multi-tick settle, only of one this
specific fix protects.

(Separately, and not what this file tests: the audit's own literal reproduction shape — a
*compiled subgraph* mounted as a node whose first superstep fans out — is confirmed unreachable
through the shipped adapter today. `tests/unit/sdk/test_langgraph_adapter.py::
test_a_mounted_subgraph_is_a_fatal_gap_rather_than_one_opaque_agent` shows a mounted subgraph is
refused at bind time with a fatal `E-INSTR-002`, before any run starts. This file's node — a
single ordinary node whose own body needs multiple real ticks before its first scheduler call —
is the general shape the fix actually protects, independent of subgraph support existing.)
"""

from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

import agentdx
from agentdx.runtime.scheduler import DeadlockError
from agentdx.sdk.generic import RunContext
from tests.unit.runtime.conftest import build_scheduler
from tests.unit.sdk.fakes import FakeHost, StampingRecorder


class _State(TypedDict, total=False):
    task: str
    plan: str


async def _settle_before_first_scheduler_call() -> None:
    """Real, multi-tick async settling with no scheduler-visible checkpoint of its own.

    The same "needs another real task to run" shape `test_root_entry_drain.py` uses for root.
    """
    done = asyncio.Event()

    async def _settle() -> None:
        done.set()

    settle_task = asyncio.ensure_future(_settle())
    await done.wait()
    await settle_task


async def _slow_node(state: _State) -> dict[str, object]:
    """An ordinary node whose body's first action needs several real ticks to settle.

    Before it does anything a scheduler recognises — mirroring a node that internally
    invokes a subgraph (or any other real, multi-tick async construct) as its first action.
    """
    await _settle_before_first_scheduler_call()
    return {"plan": "settled"}


def _build_slow_graph() -> object:
    graph = StateGraph(_State)
    graph.add_node("slow", _slow_node)
    graph.add_edge(START, "slow")
    graph.add_edge("slow", END)
    return graph.compile()


@pytest.mark.asyncio
async def test_a_spawned_node_body_survives_a_multi_tick_settle_before_its_own_first_call() -> None:
    """The fix, proven through the real, shipped adapter and a real `Scheduler`.

    `slow`'s own body needs several real ticks to settle before it reaches any scheduler-
    visible call — exactly the shape that, one level up, ADR-021 already protects for root.
    `_run_node_body`'s own `yield_point("sdk_node_entry")` must give this spawned task the
    same guaranteed checkpoint, so the settle happens on a resumption, not a first dispatch.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    context = RunContext.create(
        run_id="r_node_entry", recorder=StampingRecorder("r_node_entry"), scheduler=scheduler
    )
    host = FakeHost(context)
    graph = agentdx.instrument(_build_slow_graph(), name="probe", context=context)

    result = await scheduler.run(agentdx.run(graph, task="t", host=host))

    assert result.output == {"task": "t", "plan": "settled"}


@pytest.mark.asyncio
async def test_without_the_fix_the_identical_settle_shape_deadlocks_as_a_bare_spawn() -> None:
    """The other half of the proof: the same settle shape, spawned directly on the scheduler.

    Bypassing `_run_node_body` entirely (so its `yield_point` never runs) reproduces the exact
    mechanism `op2-audit-p06-second.md`'s `repro5_nested_immediate_fanout.py` demonstrated
    against the raw `Scheduler`: a task whose first dispatch needs several real ticks to settle,
    with nothing granting it the resumption-only drain budget, deadlocks with an empty
    `wait_reason` on the stuck task — the signature the audit traced this finding through. This
    is what makes the test above decisive rather than vacuous: the fix is specifically
    `_run_node_body`'s own extra checkpoint, not some general tolerance that would have passed
    regardless of which call path reached this settle shape.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)

    async def root() -> object:
        task_id = scheduler.spawn(_settle_before_first_scheduler_call(), agent_id="slow")
        return await scheduler.join(task_id)

    with pytest.raises(DeadlockError) as caught:
        await scheduler.run(root())
    assert "E-SCHED-003" in str(caught.value)
