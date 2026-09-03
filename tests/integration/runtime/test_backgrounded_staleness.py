"""ADR-022: `_backgrounded` no longer outlives the fan-out it was tracking.

**Found by** an independent OP-2 audit of candidate β (ADR-019, task #11), commissioned
after ADR-019 shipped to check the mechanism from outside the reasoning that built it. The
audit's one confirmed, real finding: `self._backgrounded[parent_id]` was cleared *only* by
`_drive_coro`'s own completion `finally`, once the parent's entire coroutine reached `DONE`
— never when the specific fan-out call that put it there actually closed. A parent that does
more scheduler-visible work *after* its fan-out resolves (`tester`, several steps after
`coder`/`reviewer` finish, in every one of the three real fixtures) kept a stale entry for
its own remaining execution. Not a hang by itself — `_scheduler_loop`'s grace window is
bounded either way — but a real defect: a genuinely unrelated deadlock arising later in that
same window got an unwarranted ~200-tick (`resume_drain_ticks`) grace delay it should never
have received, and `DeadlockError.wait_reasons` could end up citing a fan-out call that had
already returned.

**First fix tried and rejected.** Reference-counting: `self._call_parent: dict[str, str]`
(call_id -> parent) plus `self._backgrounded: dict[str, int]` (outstanding-mint count per
parent), decremented in `end_call` when a specific call closes, entry dropped at zero. Broke
three existing tests (this file's own predecessor scenario in `test_d62_fanout_dispatch.py`,
plus two in `test_scheduler.py`) with a *new*, different `DeadlockError` — reproduced via a
trace script: `end_call` runs on the *child's* own frame the instant its call closes, but the
*parent's* raw `await` (e.g. `asyncio.gather`) still needs a few more real event-loop ticks
after that before it actually resolves. Clearing at ref-count-zero removed the parent's
`_backgrounded` membership *before* that settling finished, so `_scheduler_loop` saw an empty
dict and declared deadlock in exactly that gap — with zero grace ticks granted, the opposite
of the original bug. The old design's "keep the entry alive until the parent's own `DONE`"
was, by accident, also the thing supplying that settling grace; ref-counting threw that away
along with the staleness.

**The actual fix (`runtime/scheduler.py`).** `_clear_backgrounded_checkin`, called from the
top of `join`, `sleep`, and `yield_point` for whichever identity is calling them: pops that
identity out of `self._backgrounded`, if present. A task's own *next* genuine scheduler call
after being backgrounded is proof — since a coroutine is sequential — that its prior raw,
unmanaged await has already fully resolved, so clearing there costs nothing (same settling
grace the old design gave, for exactly as long as it's needed) while still fixing the
staleness: the entry cannot survive past the parent's own next scheduler-visible action,
whatever that turns out to be. `begin_call`/`end_call` are unchanged from ADR-019.

**This file** proves the fix directly, extending the exact shape
`test_d62_fanout_dispatch.py::test_a_concurrent_fanout_after_a_resumption_completes_via_candidate_beta`
already covers (`planner` sequential, then `coder`/`reviewer` concurrent) with a *third*
stage — `tester`, sequential, after the fan-out resolves — mirroring every real fixture
exactly. The decisive assertion runs *from inside `tester`'s own spawned body*: at that
point root has already made its own next scheduler call (the `join` that dispatched
`tester`), so under the fix `_backgrounded` must already be empty — not merely "eventually,
once root reaches `DONE`", which is all the pre-ADR-022 design guaranteed and exactly the gap
the audit found. A second test proves the flip side: `begin_call`'s mint branch still
populates `_backgrounded` *while* a fan-out is genuinely outstanding, so this isn't a case of
the dict having quietly stopped being populated at all.
"""

from __future__ import annotations

import asyncio

import pytest

from agentdx.runtime.scheduler import Scheduler, TaskState
from tests.unit.runtime.conftest import RUN_ID, build_scheduler

_ROOT_TASK_ID = f"t_{RUN_ID[:8]}_root_0"


async def _body(name: str) -> str:
    """A trivial node body — staleness is about `_backgrounded` bookkeeping, not results."""
    return f"{name}-done"


async def _pregel_like_call(scheduler: Scheduler, name: str, agent_id: str) -> object:
    """One `run_node_async`-shaped call: `begin_call` + `spawn` + `join`, bracketed."""
    call_id = scheduler.begin_call(agent_id=agent_id)
    try:
        task_id = scheduler.spawn(_body(name), agent_id=agent_id)
        return await scheduler.join(task_id)
    finally:
        scheduler.end_call(call_id)


@pytest.mark.asyncio
async def test_backgrounded_is_cleared_by_the_time_the_next_sequential_stage_runs() -> None:
    """THE DECISIVE ONE. planner (seq) -> {coder, reviewer} (fan-out) -> tester (seq).

    Under the pre-ADR-022 design, root stays listed in `_backgrounded` for its *entire*
    remaining execution once a fan-out call mints under it — including all of `tester`'s own
    dispatch, well before root's coroutine reaches `DONE`. This test snapshots
    `scheduler._backgrounded` from *inside* `tester`'s own spawned body (dispatched only
    after root's own `join()` call for it has already run, and therefore already checked
    in) and asserts root is not there — proof the entry does not outlive the fan-out it was
    tracking, which is the exact gap the independent audit found.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    observed_backgrounded_during_tester: dict[str, None] | None = None

    async def _tester_body() -> str:
        nonlocal observed_backgrounded_during_tester
        observed_backgrounded_during_tester = dict(scheduler._backgrounded)
        return "tester-done"

    async def root() -> object:
        planner_call = scheduler.begin_call(agent_id="planner")
        try:
            planner_task = scheduler.spawn(_body("planner"), agent_id="planner")
            await scheduler.join(planner_task)
        finally:
            scheduler.end_call(planner_call)

        t1 = asyncio.ensure_future(_pregel_like_call(scheduler, "coder", "coder"))
        t2 = asyncio.ensure_future(_pregel_like_call(scheduler, "reviewer", "reviewer"))
        await asyncio.gather(t1, t2)

        tester_call = scheduler.begin_call(agent_id="tester")
        try:
            tester_task = scheduler.spawn(_tester_body(), agent_id="tester")
            return await scheduler.join(tester_task)
        finally:
            scheduler.end_call(tester_call)

    result = await asyncio.wait_for(scheduler.run(root()), timeout=10)

    assert result == "tester-done"
    assert observed_backgrounded_during_tester is not None, (
        "tester's own body never ran — this test didn't exercise the shape it claims to"
    )
    assert _ROOT_TASK_ID not in observed_backgrounded_during_tester, (
        f"{_ROOT_TASK_ID!r} was still in _backgrounded while tester ran, well before root's "
        "own DONE — ADR-022's check-in-based clearing has regressed back to the staleness "
        "bug the independent audit found (clearing tied only to the parent's own DONE)"
    )

    # No regression on the existing cleanup guarantee: once the whole run genuinely
    # completes, nothing is left in `_backgrounded` at all.
    assert not scheduler._backgrounded, (
        "a task is still marked backgrounded after the run completed"
    )


@pytest.mark.asyncio
async def test_backgrounded_is_still_populated_while_a_fanout_is_genuinely_outstanding() -> None:
    """The flip side: `_backgrounded` isn't just permanently empty now.

    Guards against a fix that "solves" staleness by never populating the dict at all (which
    would silently resurrect the original D-62 task #25 dispatch gap ADR-019 closed). Checked
    from inside `coder`'s own spawned body, while `reviewer`'s call is still outstanding
    concurrently under the same root — root must still show up in `_backgrounded` at that
    exact moment, before either fan-out call has closed.

    Includes the same `planner` sequential warm-up the decisive test above uses, so the
    fan-out happens during a *resumption* (which gets `resume_drain_ticks` grace) rather than
    root's own first dispatch (a single real tick, regardless of `_backgrounded` — the
    unrelated, already-covered failure mode
    `test_d62_fanout_dispatch.py::test_an_immediate_concurrent_fanout_with_no_prior_step_deadlocks`
    documents; without the warm-up this test hits that instead of exercising ADR-022 at all).
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    observed_backgrounded_during_coder: dict[str, None] | None = None

    async def _coder_body() -> str:
        nonlocal observed_backgrounded_during_coder
        observed_backgrounded_during_coder = dict(scheduler._backgrounded)
        return "coder-done"

    async def _coder_call() -> object:
        call_id = scheduler.begin_call(agent_id="coder")
        try:
            task_id = scheduler.spawn(_coder_body(), agent_id="coder")
            return await scheduler.join(task_id)
        finally:
            scheduler.end_call(call_id)

    async def root() -> object:
        planner_call = scheduler.begin_call(agent_id="planner")
        try:
            planner_task = scheduler.spawn(_body("planner"), agent_id="planner")
            await scheduler.join(planner_task)
        finally:
            scheduler.end_call(planner_call)

        t1 = asyncio.ensure_future(_coder_call())
        t2 = asyncio.ensure_future(_pregel_like_call(scheduler, "reviewer", "reviewer"))
        return await asyncio.gather(t1, t2)

    result = await asyncio.wait_for(scheduler.run(root()), timeout=10)

    assert result == ["coder-done", "reviewer-done"]
    assert observed_backgrounded_during_coder is not None, (
        "coder's own body never ran — this test didn't exercise the shape it claims to"
    )
    assert _ROOT_TASK_ID in observed_backgrounded_during_coder, (
        f"{_ROOT_TASK_ID!r} was missing from _backgrounded while coder ran and reviewer's "
        "call was still genuinely outstanding — begin_call's mint-branch population has "
        "regressed, which would silently reopen the original D-62 task #25 dispatch gap"
    )

    coder_calls = [tid for tid in scheduler._tasks if "_coder_" in tid]
    reviewer_calls = [tid for tid in scheduler._tasks if "_reviewer_" in tid]
    for tid in (*coder_calls, *reviewer_calls):
        assert scheduler._tasks[tid].state is TaskState.DONE

    assert not scheduler._backgrounded, (
        "a task is still marked backgrounded after the run completed"
    )
