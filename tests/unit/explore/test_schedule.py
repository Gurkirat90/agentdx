"""Unit tests for `explore/schedule.py` — hand-computed expected outputs (AGENTS.md §5)."""

from __future__ import annotations

import asyncio

import pytest

from agentdx.explore.schedule import (
    MalformedRunError,
    Turn,
    canonical_delay_schedule_text,
    signature,
    turns_from_events,
)
from tests.unit.explore._factories import schedule_decision, state_write

# ---------------------------------------------------------------------------------------
# canonical_delay_schedule_text / signature
# ---------------------------------------------------------------------------------------


def test_canonical_delay_schedule_text_is_order_independent() -> None:
    """Two mappings with the same content, built in different insertion order, match."""
    a = {3: 1, 1: 0}
    b = {1: 0, 3: 1}
    assert canonical_delay_schedule_text(a) == canonical_delay_schedule_text(b)
    assert canonical_delay_schedule_text(a) == "[[1,0],[3,1]]"


def test_canonical_delay_schedule_text_empty() -> None:
    """The default (empty) schedule has a fixed, hand-verifiable canonical text."""
    assert canonical_delay_schedule_text({}) == "[]"


def test_signature_is_deterministic_across_insertion_order() -> None:
    """Signature depends only on content, never dict insertion order (I1)."""
    assert signature({3: 1, 1: 0}) == signature({1: 0, 3: 1})


def test_signature_distinguishes_different_content() -> None:
    """Two genuinely different schedules must not collide."""
    assert signature({1: 0}) != signature({1: 1})
    assert signature({1: 0}) != signature({2: 0})


def test_signature_has_blake2b_prefix_and_fixed_length() -> None:
    """Signature format: `blake2b:` + 32 hex chars (digest_size=16 -> 16 bytes -> 32 hex)."""
    sig = signature({1: 0})
    assert sig.startswith("blake2b:")
    assert len(sig) == len("blake2b:") + 32


# ---------------------------------------------------------------------------------------
# turns_from_events
# ---------------------------------------------------------------------------------------


def test_turns_from_events_groups_by_sched_step_in_order() -> None:
    """Two turns: a solo-runnable root, then a two-way choice with one observable write."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="root", ready_task_ids=[]),
        schedule_decision(seq=1, sched_step=2, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=2, sched_step=2, agent_id="a", key="k", value_hash="vh1"),
    )
    turns = turns_from_events(events)
    assert len(turns) == 2

    assert turns[0] == Turn(sched_step=1, chosen_task_id="root", ready_task_ids=(), events=())
    assert turns[1].sched_step == 2
    assert turns[1].chosen_task_id == "a"
    assert turns[1].ready_task_ids == ("b",)
    assert turns[1].events == (events[2],)
    assert turns[1].choices_at == 2
    assert turns[1].candidate_task_ids == ("a", "b")
    assert turns[1].decision_step == 1  # sched_step - 1


def test_turns_from_events_ready_task_ids_sorted_candidate_order() -> None:
    """`candidate_task_ids` is chosen-first, then the rest sorted — never insertion order."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="c", ready_task_ids=["z", "a"]),
    )
    turns = turns_from_events(events)
    assert turns[0].candidate_task_ids == ("c", "a", "z")


def test_turns_from_events_empty_log_returns_no_turns() -> None:
    """A log with zero `schedule_decision` events reconstructs zero turns (not an error)."""
    assert turns_from_events(()) == ()


def test_turns_from_events_raises_on_conflicting_chosen_task_id() -> None:
    """Two `schedule_decision`s at the same `sched_step` naming different chosen tasks."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),
        schedule_decision(seq=1, sched_step=1, chosen_task_id="b", ready_task_ids=[]),
    )
    with pytest.raises(MalformedRunError):
        turns_from_events(events)


def test_turns_from_events_duplicate_identical_decision_is_not_an_error() -> None:
    """Two `schedule_decision`s at the same step naming the *same* chosen task is fine."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=["b"]),
        schedule_decision(seq=1, sched_step=1, chosen_task_id="a", ready_task_ids=["b"]),
    )
    turns = turns_from_events(events)
    assert len(turns) == 1


# ---------------------------------------------------------------------------------------
# decision_step vs. sched_step — the off-by-one regression test
# ---------------------------------------------------------------------------------------


def test_decision_step_matches_live_scheduler() -> None:
    """`Turn.decision_step` is the `DelaySchedule` key that actually controls that turn.

    Caught during P13's own build: `Scheduler._choose()` reads `self._step` *before* it is
    incremented, but the `schedule_decision` event for that same turn is stamped with
    `self._step` *after* the increment (`_scheduler_loop`: `chosen = self._choose(...)`,
    `self._step += 1`, `await self._resume_task(chosen)`). So the turn recorded with
    `sched_step=N` was actually decided by the `_choose()` call made when `self._step ==
    N - 1` — using `sched_step` directly as a `DelaySchedule` key silently steers the
    *next* turn instead. This test proves `decision_step` (`= sched_step - 1`) is the
    correct key against a real `Scheduler`, not just reasoned about from the source.
    """
    from agentdx.config import SchedulerConfig
    from agentdx.events.writer import EventWriter
    from agentdx.runtime.clock import VirtualClock
    from agentdx.runtime.scheduler import Scheduler
    from tests.unit.runtime.conftest import MemorySink

    async def worker(scheduler: Scheduler, task_id: str, n: int) -> None:
        for i in range(n):
            await scheduler.yield_point(f"{task_id}_step_{i}")

    async def root(scheduler: Scheduler) -> None:
        scheduler.spawn(worker(scheduler, "a", 3), agent_id="a")
        scheduler.spawn(worker(scheduler, "b", 3), agent_id="b")

    def run(delay_schedule: dict[int, int] | None) -> tuple:
        clock = VirtualClock()
        sink = MemorySink()
        writer = EventWriter("r_offbyone", sink, batch_size=1)
        config = SchedulerConfig(strict_determinism=True, step_budget=100_000)
        scheduler = Scheduler(
            run_id="r_offbyone",
            seed=1,
            clock=clock,
            writer=writer,
            config=config,
            policy="priority",
            delay_schedule=delay_schedule,
        )
        asyncio.run(scheduler.run(root(scheduler)))
        return sink.events()

    default_events = run(None)
    default_turns = turns_from_events(default_events)
    # First two-way choice: `a` vs `b`, priority policy picks `a` by default.
    first_branch = next(t for t in default_turns if t.choices_at > 1)
    assert first_branch.chosen_task_id.endswith("_a_0")

    # Steer that exact turn to `b` using decision_step (the sanctioned conversion).
    steered_events = run({first_branch.decision_step: 1})
    steered_turns = turns_from_events(steered_events)
    steered_first_branch = next(t for t in steered_turns if t.choices_at > 1)
    assert steered_first_branch.sched_step == first_branch.sched_step
    assert steered_first_branch.chosen_task_id.endswith("_b_0")

    # The naive (buggy) mapping — using sched_step directly — must NOT steer this turn;
    # it silently steers the next one instead, which is exactly the bug this regression
    # test exists to keep fixed.
    naive_events = run({first_branch.sched_step: 1})
    naive_turns = turns_from_events(naive_events)
    naive_first_branch = next(t for t in naive_turns if t.choices_at > 1)
    assert naive_first_branch.chosen_task_id.endswith("_a_0")  # unaffected, as expected
