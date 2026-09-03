"""ADR-021: root's own first dispatch now survives a multi-tick graph-entry ceremony.

CONTEXT.md §8 ADR-021, found 2026-09-03 while re-verifying D-80/ADR-020 on real Python 3.12
hardware. `agentdx run fixtures/code_pipeline --task fixtures/tasks/refactor_module.md
--seed 42` deadlocked (`E-SCHED-003`, empty `wait_reason`, only the root task listed) on its
very first invocation — before candidate beta's own fan-out dispatch gap (ADR-019) was ever
reached, and before `sdk/langgraph.py::run_node_async` ever called `spawn()` for the graph's
first node at all (confirmed by a `RuntimeWarning: coroutine 'LangGraphAdapter._run_node_body'
was never awaited` — that coroutine was created but never handed to the scheduler).

**Root cause.** `Scheduler._resume_task` deliberately gives a task's *first* dispatch exactly
one real event-loop tick (`d62-design.md` §2-3) — it has not yet proven it returns to a
suspension the scheduler itself created, so it is not eligible for the `resume_drain_ticks`
grace period a *resumption* gets. `agentdx.run()` (`sdk/generic.py`) — the coroutine driven as
the scheduler's own root task — has to get from its own start, through
`_invoke(graph, payload)`, into LangGraph's own Pregel/`ainvoke()` entry machinery, before it
ever reaches this SDK's first `spawn()`/`join()` call (for the graph's first node). That entry
machinery is real, third-party async ceremony this codebase does not control — the same class
of `@shielded`-callback overhead `_resume_task`'s own docstring already documents as needing
several real ticks to settle on a resumption — and on real hardware it does not reliably
finish inside root's single first-dispatch tick. This did not reproduce on a Python 3.10
sandbox (5/5 passes), so this gap was never exercised by ADR-019's own validation, which ran
exclusively there.

**A blanket fix was tried first and rejected.** Granting root's *entire* first dispatch the
`resume_drain_ticks` grace period (unconditionally, whenever `task.agent_id == "root"`) does
make the real repro pass — but it also silently breaks
`test_d62_suspension_contract.py::test_a_suspension_needing_another_task_deadlocks`: with
extra real ticks available, that test's own raw, unmanaged `asyncio.Event`/`ensure_future`
suspension gets enough ticks to actually resolve, so the scheduler no longer deadlocks on it
— defeating the exact non-determinism guard that decisive control exists to enforce (I1: a
root-level suspension on real, unmanaged concurrency must never be tolerated just because it
happens to resolve in time on some real hardware). A fix that passes the real repro by
breaking that control is not a fix.

**The actual fix (`sdk/generic.py::run`).** Root is a *known*, fixed shape — always the same
composition (`open_run`, then the instrumented graph), never arbitrary user code — so instead
of loosening the first-dispatch rule, `agentdx.run()` now calls
`await context.scheduler.yield_point("sdk_run_entry")` once, unconditionally, immediately
after `open_run()` returns and before the graph is ever invoked. This gives root exactly one
legitimate, scheduler-recognised checkpoint before it ever reaches LangGraph's own machinery,
so that machinery's own multi-tick settling happens on a *resumption* — which already, safely,
tolerates precisely this class of real async overhead — never on a first dispatch. Every
first-dispatch safety property (this file's own decisive control included) is completely
unchanged; `_resume_task`/`_scheduler_loop` are not touched at all.

**This file** proves the fix directly: a `graph` stand-in whose `ainvoke()` reproduces the
same "needs another real task to run" shape `test_a_suspension_needing_another_task_deadlocks`
uses, immediately followed by a genuine `scheduler.spawn()`/`join()` call — so the assertion is
not merely "did not deadlock" but "root's own first spawn()/join() call actually happened and
returned its result". `test_without_the_fix_this_exact_shape_deadlocks` is the other half:
the same coroutine, driven directly (bypassing `agentdx.run()`, so the `yield_point` call never
happens), still deadlocks — proving this file's own "survives" test is not vacuously true of
any multi-tick entry, only of one root has been given a real chance to settle through.
"""

from __future__ import annotations

import asyncio

import pytest

import agentdx
from agentdx.runtime.scheduler import DeadlockError
from agentdx.sdk.generic import RunContext
from tests.unit.runtime.conftest import build_scheduler
from tests.unit.sdk.fakes import FakeHost, StampingRecorder


class _SlowEntryGraph:
    """Mimics LangGraph's own Pregel/`ainvoke()` entry ceremony.

    Real, multi-tick async settling — needing the event loop to run a separate real task —
    before it ever reaches a scheduler-recognised call, then a genuine `spawn()`/`join()`
    for the graph's first node. The settling shape is deliberately identical to
    `test_d62_suspension_contract.py::_awaits_work_done_by_another_real_task` — the same
    "needs another task to run" suspension that file's own decisive control proves the
    scheduler must never silently tolerate on a first dispatch.
    """

    def __init__(self, scheduler: object) -> None:
        self._scheduler = scheduler

    async def ainvoke(self, payload: object) -> object:
        done = asyncio.Event()

        async def _settle() -> None:
            done.set()

        settle_task = asyncio.ensure_future(_settle())
        await done.wait()
        await settle_task

        async def _node_body() -> str:
            return "node ran"

        task_id = self._scheduler.spawn(_node_body(), agent_id="probe")  # type: ignore[attr-defined]
        return await self._scheduler.join(task_id)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_root_survives_a_multi_tick_graph_entry_before_its_first_spawn_or_join() -> None:
    """ADR-021, proven directly.

    Root's own first dispatch now tolerates the same multi-tick, "needs another real task to
    run" settling `d62-design.md` documents LangGraph's own entry machinery as needing —
    reaching, and returning the result of, its first genuine `spawn()`/`join()` call rather
    than deadlocking on the settling itself.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    # `tests.unit.sdk.fakes.make_context` has no `scheduler=` override — build the context
    # directly so this test can wire in the *real* Scheduler `agentdx.run()`'s root
    # coroutine actually runs under (the whole point: this is the real spawn()/join() path,
    # not the default no-op stub `RunContext.create` otherwise falls back to).
    context = RunContext.create(
        run_id="r_root_entry", recorder=StampingRecorder("r_root_entry"), scheduler=scheduler
    )
    host = FakeHost(context)
    graph = _SlowEntryGraph(scheduler)

    result = await scheduler.run(agentdx.run(graph, task="t", host=host))

    assert result.output == "node ran"


@pytest.mark.asyncio
async def test_without_the_fix_this_exact_shape_deadlocks() -> None:
    """The other half of the proof: the same shape, but bypassing `agentdx.run()`.

    Driven directly (so the ADR-021 `yield_point` call never happens), this still deadlocks
    exactly as `test_a_suspension_needing_another_task_deadlocks` does. This is what makes
    the test above decisive rather than vacuous: the fix is specifically `agentdx.run()`'s
    own extra checkpoint, not some general tolerance for multi-tick entries that would have
    passed regardless.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    graph = _SlowEntryGraph(scheduler)

    with pytest.raises(DeadlockError) as caught:
        await scheduler.run(graph.ainvoke({"task": "t"}))
    assert "E-SCHED-003" in str(caught.value)
