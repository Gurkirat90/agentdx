"""D-62, task #25 follow-up: does ADR-018 alone let a Pregel-style fan-out complete?

**An experiment, not a regression suite** — same treatment as
`test_d62_suspension_contract.py`'s own decisive test, and for the same reason.

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

**What this file checks.** ADR-018 is validated for identity correctness by
`tests/unit/runtime/test_scheduler.py`'s own "begin_call / end_call identity" section,
directly and without touching this question. What remains open, per `d62-design.md` §8.3, is
a *second*, separate bug the identity fix does not touch: `_scheduler_loop` never regains
control to dispatch a fan-out superstep's spawned node-body tasks while the parent is stuck
mid-drain on Pregel's own raw `asyncio.gather`/`asyncio.wait` — because that raw await never
touches the parent's own `Task.state`, and `_scheduler_loop`'s next iteration cannot start
until the *current* `_resume_task(parent)` call returns. This file is the decisive,
tracked probe for that gap: it reproduces the exact shape the real fixtures hit (a
sequential warm-up step, mirroring `run_node_async`'s fast path, followed immediately by a
Pregel-style concurrent fan-out, mirroring `PregelRunner.atick`'s `self.submit()` path) and
records what happens.

    just test tests/integration/runtime/test_d62_fanout_dispatch.py -v -m experiment

**RESULT, 2026-09-03 (this file; also `probe_fanout.py`, a standalone scratch harness this
file promotes into a tracked test).** The gap is real and reproducible: root's own drain
window (`SchedulerConfig.resume_drain_ticks`, default 200 real event-loop ticks) is
exhausted with zero progress on either spawned node body, and the scheduler raises
`SchedulerError` (`E-SCHED-001`) rather than completing. **Both spawned bodies remain
`PENDING`** in `self._tasks` the entire time — real, correctly and distinctly identified
(proof the identity fix already did its job), entirely inert, because `_scheduler_loop`
never gets a turn to pick them up. All three reference fixtures reproduce this same failure
end-to-end and remain blocked pending a choice among `d62-design.md` §8.4's three candidates
(none chosen as of this writing).

**Why this is `@pytest.mark.experiment` and not a green completion test.** It is written to
fail by design while the dispatch gap is open — the same discipline
`test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all` uses next door, and the
same reason: an unmarked member of the default suite going red for a reason no single commit
can fix is exactly what `CONTEXT.md` §11 tripwire 15 exists to catch. Whoever closes the
dispatch gap (chooses and builds one of §8.4's candidates) updates or removes this file's own
expectations — that is what removes the marker, not a CI exemption to keep.

**Why `strict_determinism=False`.** Same reasoning as `test_d62_suspension_contract.py`:
this experiment deliberately does not control the raw `asyncio.Task`s it creates to mimic
Pregel's `self.submit()` path, and `strict=True` would raise `DeterminismLeakError` on them
before the scheduler loop is even reached, letting a different failure impersonate the one
under test.
"""

from __future__ import annotations

import asyncio

import pytest

from agentdx.runtime.scheduler import DeadlockError, Scheduler, SchedulerError, TaskState
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

    async def root() -> tuple[object, object]:
        t1 = asyncio.ensure_future(_pregel_like_call(scheduler, "coder", "coder"))
        t2 = asyncio.ensure_future(_pregel_like_call(scheduler, "reviewer", "reviewer"))
        return await asyncio.gather(t1, t2)

    with pytest.raises(DeadlockError):
        await scheduler.run(root())


@pytest.mark.experiment
@pytest.mark.asyncio
async def test_a_concurrent_fanout_after_a_resumption_hits_the_dispatch_gap() -> None:
    """THE DECISIVE ONE. Sequential step, then fan out — the shape every real fixture hits.

    Root first runs one node sequentially via its own `begin_call`+`spawn`+`join` (mirroring
    Pregel's single-ready-node fast path), exactly as `run_node_async` does it. Resumed from
    that `join` — a genuine scheduler-tracked resumption, which is what actually grants the
    `resume_drain_ticks` budget — root then fans out into two concurrent, real, raw
    `asyncio.Task`s (mirroring `PregelRunner.atick`'s `self.submit()`), each independently
    calling `begin_call`+`spawn`+`join`. This is the exact timing `fixtures/code_pipeline`
    and the other two reference fixtures hit.

    **Fails as documented (`SchedulerError`, drain budget exhausted)** → the dispatch gap is
    still open, matching `d62-design.md` §8.3 exactly; this test's own diagnostic assertions
    below additionally confirm the identity fix (ADR-018) is *not* what's blocking
    completion — `coder` and `reviewer` minted distinct identities and neither orphaned the
    other, they simply never got dispatched.

    **Completes instead** → one of `d62-design.md` §8.4's candidates has been built (or the
    gap closed some other way) and this test's own expectations are stale; update this file
    (drop the `pytest.raises` and the marker) rather than leaving a passing test asserting a
    bug that no longer exists.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)

    async def root() -> tuple[object, object]:
        planner_call = scheduler.begin_call(agent_id="planner")
        try:
            planner_task = scheduler.spawn(_body("planner"), agent_id="planner")
            await scheduler.join(planner_task)
        finally:
            scheduler.end_call(planner_call)

        t1 = asyncio.ensure_future(_pregel_like_call(scheduler, "coder", "coder"))
        t2 = asyncio.ensure_future(_pregel_like_call(scheduler, "reviewer", "reviewer"))
        return await asyncio.gather(t1, t2)

    with pytest.raises(SchedulerError) as caught:
        await asyncio.wait_for(scheduler.run(root()), timeout=10)

    # HYPOTHESIS CONFIRMED (this failure is the finding, not a bug in the test): the
    # dispatch gap, not the identity collision, is what stops this from completing.
    assert "E-SCHED-001" in str(caught.value)
    assert not isinstance(caught.value, DeadlockError), (
        "a DeadlockError here would mean root never even reached the fan-out step — a "
        "harness regression, not the dispatch-gap finding this test documents"
    )

    # The identity claim, checked at the point of failure: coder and reviewer each got a
    # real, distinct, correctly-owned call — ADR-018 did its job. Neither is DONE (dispatch
    # never reached them), and neither collided with or orphaned the other's bookkeeping.
    coder_calls = [tid for tid in scheduler._tasks if "_coder_" in tid]
    reviewer_calls = [tid for tid in scheduler._tasks if "_reviewer_" in tid]
    assert coder_calls, "coder's begin_call-minted identity is missing entirely"
    assert reviewer_calls, "reviewer's begin_call-minted identity is missing entirely"
    assert set(coder_calls).isdisjoint(reviewer_calls), (
        "coder and reviewer share a task id — this IS the identity collision task #25 "
        "found and ADR-018 was built to close; if this fires, ADR-018 has regressed"
    )
    for tid in (*coder_calls, *reviewer_calls):
        assert scheduler._tasks[tid].state is not TaskState.DONE, (
            f"{tid} reached DONE — the dispatch gap this test documents appears closed; "
            "see this test's own docstring for what to do next"
        )
