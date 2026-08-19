"""Unit tests for `explore/generate.py` — a hand-built, deterministic fake `ScheduleExecutor`.

No real `Scheduler` here (that is `tests/integration/explore/`'s job) — this module's own
BFS/termination/dedup/reduction-wiring logic is tested against a pure function of
`delay_schedule`, so every expected count below is hand-computable, not merely observed
(AGENTS.md §5).

**The fixed fake scenario.** Two independent binary decision points, always present
regardless of which choice was taken: turn 1 (`sched_step=1`, `decision_step=0`) picks `x` or
`y`; turn 2 (`sched_step=2`, `decision_step=1`) picks `p` or `q`, unconditionally. Each choice
writes to its own, never-reused key, so `reduce.interesting_steps` always fails open (neither
candidate at either turn is ever observed taking an earlier turn in the same run) — every
multi-choice point stays interesting, so this fixture also pins down `pruned_by_reduction ==
0` as a known-good baseline, distinct from `test_reduce.py`'s own reduction-focused cases.
"""

from __future__ import annotations

import time

from agentdx.explore.generate import Budget, explore
from agentdx.explore.schedule import DelaySchedule, canonical_delay_schedule_text
from tests.unit.explore._factories import schedule_decision, state_write


def _fake_executor(delay_schedule: DelaySchedule) -> tuple:
    """Pure function of `delay_schedule` — same input always returns the identical log."""
    step0 = delay_schedule.get(0, 0) % 2
    chosen1, other1 = ("x", "y") if step0 == 0 else ("y", "x")
    step1 = delay_schedule.get(1, 0) % 2
    chosen2, other2 = ("p", "q") if step1 == 0 else ("q", "p")
    return (
        schedule_decision(seq=0, sched_step=1, chosen_task_id=chosen1, ready_task_ids=[other1]),
        state_write(seq=1, sched_step=1, agent_id=chosen1, key=f"k_{chosen1}", value_hash="vh"),
        schedule_decision(seq=2, sched_step=2, chosen_task_id=chosen2, ready_task_ids=[other2]),
        state_write(seq=3, sched_step=2, agent_id=chosen2, key=f"k_{chosen2}", value_hash="vh"),
    )


def test_explore_full_frontier_hand_computed_counts() -> None:
    """Verified ground truth (see module docstring): 4 unique schedules, 3 duplicates.

    The 3 duplicates: {0:1} re-branching at turn 1 (decision_step=0, already pinned to 1)
    reproduces {0:1} itself; {1:1} re-branching at turn 2 (decision_step=1, already pinned)
    reproduces {1:1} itself; {1:1} branching at turn 1 produces {0:1,1:1} by content, already
    seen via {0:1}'s own turn-2 branch. Re-picking an already-pinned decision point is a
    known, accepted v1 BFS cost (`generate.py`'s own docstring) — caught correctly by dedup,
    never executed twice.
    """
    result = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=30.0)
    assert len(result.results) == 4
    assert result.unique_signatures == 4
    assert result.pruned_by_reduction == 0
    assert result.duplicate_children == 3
    assert result.capped is False
    assert result.budget_exceeded is False
    schedules = [dict(r.delay_schedule) for r in result.results]
    assert schedules == [{}, {0: 1}, {1: 1}, {0: 1, 1: 1}]


def test_explore_default_schedule_always_executed_first() -> None:
    """BFS by delay count: the empty (default) schedule is always the first executed."""
    result = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=30.0)
    assert result.results[0].delay_schedule == {}


def test_explore_delay_bound_k_zero_executes_only_the_default() -> None:
    """k=0: no child is ever generated, regardless of how many interesting turns exist."""
    result = explore(_fake_executor, delay_bound_k=0, schedule_cap_n=200, time_budget_s=30.0)
    assert len(result.results) == 1
    assert result.results[0].delay_schedule == {}
    assert result.pruned_by_reduction == 0
    assert result.duplicate_children == 0


def test_explore_delay_bound_k_one_stops_at_depth_one() -> None:
    """k=1: only the default plus its direct children — no depth-2 schedule is ever built."""
    result = explore(_fake_executor, delay_bound_k=1, schedule_cap_n=200, time_budget_s=30.0)
    schedules = [dict(r.delay_schedule) for r in result.results]
    assert schedules == [{}, {0: 1}, {1: 1}]
    assert result.duplicate_children == 0  # no re-branch of an already-pinned point at k=1


def test_explore_schedule_cap_n_truncates_and_reports_capped() -> None:
    """N bounds executions even when the frontier is not yet empty."""
    result = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=2, time_budget_s=30.0)
    assert len(result.results) == 2
    assert result.capped is True
    assert result.budget_exceeded is False


def test_explore_cap_reached_never_sets_budget_exceeded() -> None:
    """Regression: hitting N must never also flip `budget_exceeded`.

    This is the honesty bug found and fixed during P13's own build — see `Budget`'s
    docstring. A generous wall-clock budget makes it unambiguous that only the cap could
    have stopped this run.
    """
    result = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=1, time_budget_s=9999.0)
    assert result.capped is True
    assert result.budget_exceeded is False


def test_explore_natural_completion_at_exactly_n_is_not_capped() -> None:
    """The full frontier (4 schedules) explored with N set to exactly 4.

    Nothing was truncated, so `capped` must be False even though
    `schedules_executed == schedule_cap_n`.
    """
    result = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=4, time_budget_s=30.0)
    assert len(result.results) == 4
    assert result.capped is False
    assert result.budget_exceeded is False


def test_explore_time_budget_exceeded_reports_honestly() -> None:
    """A near-zero time budget stops the search almost immediately, and says so."""

    def slow_executor(delay_schedule: DelaySchedule) -> tuple:
        time.sleep(0.05)
        return _fake_executor(delay_schedule)

    result = explore(slow_executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=0.01)
    assert result.budget_exceeded is True
    assert result.capped is False  # the cap (200) was nowhere close; only the clock stopped this
    assert len(result.results) < 4  # did not reach the full frontier


def test_explore_is_deterministic_across_repeated_calls() -> None:
    """Same fake executor, same bounds -> identical explored-schedule sequence, every time."""
    first = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=30.0)
    for _ in range(5):
        again = explore(_fake_executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=30.0)
        assert [canonical_delay_schedule_text(r.delay_schedule) for r in again.results] == [
            canonical_delay_schedule_text(r.delay_schedule) for r in first.results
        ]


def test_budget_start_and_exceeded() -> None:
    """`Budget` in isolation is wall-clock only — it knows nothing about the schedule cap."""
    generous = Budget.start(time_budget_s=9999.0)
    assert generous.exceeded() is False

    immediate = Budget.start(time_budget_s=0.0)
    time.sleep(0.01)
    assert immediate.exceeded() is True
