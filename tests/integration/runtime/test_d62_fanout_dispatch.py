"""D-62, task #25 follow-up: does a Pregel-style fan-out complete under the real Scheduler?

**Status (2026-09-03): the dispatch gap this file documented is closed, by candidate β
(ADR-019).** The decisive test below now asserts *completion*, not the `SchedulerError` it
used to assert while the gap was open — this file's own original docstring (preserved below,
unedited, as the historical record of what was found and why the test was shaped this way)
said exactly this would happen: "update this file (drop the `pytest.raises` and the marker)
rather than leaving a passing test asserting a bug that no longer exists." ADR-019's own
`CONTEXT.md` §8 row said validating candidate β "was real CLI runs read back from SQLite, not
yet a tracked regression test" — this rewrite is that promotion, owed since ADR-019 shipped.

**Where this picks up.** `test_d62_suspension_contract.py` settled that fan-out itself is
not what deadlocks the scheduler — any suspension whose resolution needs another task to
run does, fan-out or not. Implementing D-62 Option B against the real fixtures (all three of
which do fan out: `planner -> {coder, reviewer} -> tester`) then found a second, more
specific bug: `join()`'s ambient caller-identity resolution collides the moment Pregel
dispatches two or more ready nodes concurrently in one superstep, because every hidden
`asyncio.Task` Pregel creates for `self.submit()` inherits the *same* `SchedTaskContext` its
parent had ambient. That is task #25, audited in `CONTEXT.md` §13 (2026-09-03) and closed —
for the identity collision specifically — by ADR-018 (`Scheduler.begin_call`/`end_call`,
`runtime/scheduler.py`).

**Original docstring, preserved as history (what this file checked before ADR-019).** ADR-018
is validated for identity correctness by `tests/unit/runtime/test_scheduler.py`'s own
"begin_call / end_call identity" section, directly and without touching this question. What
remained open, per `d62-design.md` §8.3, was a *second*, separate bug the identity fix did not
touch: `_scheduler_loop` never regained control to dispatch a fan-out superstep's spawned
node-body tasks while the parent was stuck mid-drain on Pregel's own raw
`asyncio.gather`/`asyncio.wait` — because that raw await never touched the parent's own
`Task.state`, and `_scheduler_loop`'s next iteration could not start until the *current*
`_resume_task(parent)` call returned. This file was the decisive, tracked probe for that gap:
it reproduces the exact shape the real fixtures hit (a sequential warm-up step, mirroring
`run_node_async`'s fast path, followed immediately by a Pregel-style concurrent fan-out,
mirroring `PregelRunner.atick`'s `self.submit()` path).

**Original RESULT, 2026-09-03 (before candidate β; also `probe_fanout.py`, a standalone
scratch harness this file originally promoted into a tracked test).** The gap was real and
reproducible: root's own drain window (`SchedulerConfig.resume_drain_ticks`, default 200 real
event-loop ticks) was exhausted with zero progress on either spawned node body, and the
scheduler raised `SchedulerError` (`E-SCHED-001`) rather than completing. Both spawned bodies
remained `PENDING` in `self._tasks` the entire time — real, correctly and distinctly
identified (proof the identity fix already did its job), entirely inert, because
`_scheduler_loop` never got a turn to pick them up.

**Candidate β's mechanism (ADR-019, `runtime/scheduler.py`), what closes the gap this test
now confirms.** `begin_call`'s mint branch flips the calling task's identity to `BLOCKED`
(tracked in `self._backgrounded`) whenever it is currently `RUNNING` at the moment a nested
fan-out call mints a fresh identity — this hands control back to `_scheduler_loop` instead of
leaving the parent stuck mid-drain, so the ordinary next-iteration `_collect_runnable`/
`_choose` path picks up the newly spawned fan-out bodies through the *same* top-level dispatch
every other task uses. `_drive_coro`'s completion `finally` clears `_backgrounded` membership
only once the parent's own real `asyncio.Task` genuinely reaches `DONE`. `_scheduler_loop`'s
deadlock branch grants real ticks up to `resume_drain_ticks` before declaring deadlock
whenever `self._backgrounded` is non-empty, so the parent's own real, unmanaged
`asyncio.gather` gets the ticks it needs to settle. Full design history — the two rejected
candidates, the reasoning for choosing β over the other live candidate — is in
`d62-design.md` §8.6/§8.7, not restated here.

    just test tests/integration/runtime/test_d62_fanout_dispatch.py -v

**Why `strict_determinism=False`.** Same reasoning as `test_d62_suspension_contract.py`:
this test deliberately does not control the raw `asyncio.Task`s it creates to mimic Pregel's
`self.submit()` path, and `strict=True` would raise `DeterminismLeakError` on them before the
scheduler loop is even reached, letting a different failure impersonate the one under test.
"""

from __future__ import annotations

import asyncio

import pytest

from agentdx.runtime.scheduler import DeadlockError, Scheduler, TaskState
from tests.unit.runtime.conftest import build_scheduler


async def _body(name: str) -> str:
    """A trivial node body — the fan-out gap is about dispatch, not what runs once dispatched."""
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
async def test_an_immediate_concurrent_fanout_with_no_prior_step_deadlocks() -> None:
    """CONTROL. Fan out on root's very *first* tick — no prior scheduler-tracked suspension.

    Root's first dispatch grants exactly one real tick (`_drive_coro`'s first-dispatch path,
    not the `resume_drain_ticks` budget a *resumption* gets — see
    `test_scheduler.py::_warm_up_with_a_sequential_call`). One tick is not enough for even a
    trivial hidden task to run and settle, so this hits `_scheduler_loop`'s own
    `DeadlockError` (`E-SCHED-003`) immediately, before either hidden task runs a single
    line — the *other* failure mode `d62-design.md` §8.3 records, distinct from the decisive
    test below. Not marked `experiment`: this is the same well-understood, already-settled
    "suspension needing another task to run" boundary `test_d62_suspension_contract.py`
    establishes, reached here via a different door (a raw fan-out) rather than a new claim.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)

    async def root() -> object:
        t1 = asyncio.ensure_future(_pregel_like_call(scheduler, "coder", "coder"))
        t2 = asyncio.ensure_future(_pregel_like_call(scheduler, "reviewer", "reviewer"))
        return await asyncio.gather(t1, t2)

    with pytest.raises(DeadlockError):
        await scheduler.run(root())


@pytest.mark.asyncio
async def test_a_concurrent_fanout_after_a_resumption_completes_via_candidate_beta() -> None:
    """THE DECISIVE ONE. Sequential step, then fan out — the shape every real fixture hits.

    Root first runs one node sequentially via its own `begin_call`+`spawn`+`join` (mirroring
    Pregel's single-ready-node fast path), exactly as `run_node_async` does it. Resumed from
    that `join` — a genuine scheduler-tracked resumption, which is what actually grants the
    `resume_drain_ticks` budget — root then fans out into two concurrent, real, raw
    `asyncio.Task`s (mirroring `PregelRunner.atick`'s `self.submit()`), each independently
    calling `begin_call`+`spawn`+`join`. This is the exact timing `fixtures/code_pipeline`
    and the other two reference fixtures hit.

    Formerly the decisive *failing* probe for the dispatch gap (`SchedulerError`, drain
    budget exhausted — see this file's own preserved "Original RESULT" above); now, with
    candidate β built (ADR-019), the decisive *regression guard* that it stays closed:
    completes cleanly, both spawned bodies actually run and reach `DONE`, and — unchanged
    from the original version of this test — `coder` and `reviewer` still mint distinct,
    non-colliding identities (ADR-018's own claim, which this test has always also covered
    and must keep covering).
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)

    async def root() -> object:
        planner_call = scheduler.begin_call(agent_id="planner")
        try:
            planner_task = scheduler.spawn(_body("planner"), agent_id="planner")
            await scheduler.join(planner_task)
        finally:
            scheduler.end_call(planner_call)

        t1 = asyncio.ensure_future(_pregel_like_call(scheduler, "coder", "coder"))
        t2 = asyncio.ensure_future(_pregel_like_call(scheduler, "reviewer", "reviewer"))
        return await asyncio.gather(t1, t2)

    result = await asyncio.wait_for(scheduler.run(root()), timeout=10)

    # THE CLAIM UNDER TEST: candidate β's dispatch-gap fix, still holding. Both fanned-out
    # node bodies actually ran to completion and their results made it all the way back
    # through join() -> gather() -> the root coroutine's own return value.
    assert result == ["coder-done", "reviewer-done"]

    # The identity claim (ADR-018, unchanged by this rewrite): coder and reviewer each got
    # a real, distinct, correctly-owned call, and neither collided with or orphaned the
    # other's bookkeeping — checked after completion, same assertion this test has always
    # made, just no longer paired with an expectation that dispatch never reached them.
    coder_calls = [tid for tid in scheduler._tasks if "_coder_" in tid]
    reviewer_calls = [tid for tid in scheduler._tasks if "_reviewer_" in tid]
    assert coder_calls, "coder's begin_call-minted identity is missing entirely"
    assert reviewer_calls, "reviewer's begin_call-minted identity is missing entirely"
    assert set(coder_calls).isdisjoint(reviewer_calls), (
        "coder and reviewer share a task id — this IS the identity collision task #25 "
        "found and ADR-018 was built to close; if this fires, ADR-018 has regressed"
    )
    for tid in (*coder_calls, *reviewer_calls):
        assert scheduler._tasks[tid].state is TaskState.DONE, (
            f"{tid} never reached DONE — candidate β's dispatch-gap fix (ADR-019) appears "
            "to have regressed; see this file's own preserved history for what that looks "
            "like (SchedulerError, drain budget exhausted, both bodies stuck PENDING)"
        )

    # Cleanup claim (ADR-019's own docstring, `_drive_coro`'s finally block): once every
    # backgrounded task genuinely reaches DONE through its own real completion path,
    # nothing is left in `_backgrounded` — a leaked entry here would hang a *later* run's
    # own `_has_remaining_tasks` on a task that will never complete a second time.
    assert not scheduler._backgrounded, (
        "a task is still marked backgrounded after the run completed — ADR-019's own "
        "cleanup guarantee (cleared only in _drive_coro's finally, once DONE) has regressed"
    )
