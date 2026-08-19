"""explore/generate.py — schedule generation (PRD §15.3) and termination (PRD §15.5).

`explore()` is PRD §15.3's pseudocode, executed: breadth-first by delay count (fewest
deviations first — "it finds the simplest reproducing schedule first, which is also the most
useful one for the user"), pruned by `reduce.interesting_steps`, deduplicated by
`dedup.SeenSchedules`, and bounded by three independent termination conditions (PRD §15.5):
`k` bounds tree depth (a child is never generated past depth `k`), `N` bounds executions,
`time_budget_s` bounds wall clock. Every one of the three is checked; none is trusted to imply
another.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agentdx import wall_time
from agentdx.events.schema import Event
from agentdx.explore.dedup import SeenSchedules
from agentdx.explore.reduce import interesting_steps
from agentdx.explore.schedule import DelaySchedule, Turn, turns_from_events


@runtime_checkable
class ScheduleExecutor(Protocol):
    """Executes one delay schedule to completion and returns its event log.

    This is PRD §15.3's `execute(root_run.scenario, root_run.seed, delay_schedule=ds)`,
    injected rather than implemented here. `explore/` is free to import `runtime` (the
    import-linter `explore-below-transport` contract places no restriction on it), but *which*
    scenario and seed to run, and how to spawn its tasks under a `Scheduler`, is caller-
    specific — a scenario file, a LangGraph fixture, a hand-built test harness. Binding one
    concrete choice into this module would make every other caller re-implement the BFS
    instead of supplying an executor, and would make this module untestable without a live
    scheduler. See `docs/exploration.md` "Why execution is injected".
    """

    def __call__(self, delay_schedule: DelaySchedule) -> tuple[Event, ...]:
        """Run the bound root scenario at the bound seed, deviating per `delay_schedule`.

        Returns:
            The complete event log for this one execution, in `seq` order. Design Constraint
            4 (seeded, reproducible) is this callable's own obligation: the same
            `delay_schedule` must return the same log, byte for byte in its canonical
            projection, on every call.
        """
        ...


@dataclass(frozen=True, slots=True)
class Budget:
    """The wall-clock half of PRD §15.5's three termination bounds — nothing else.

    `k` and `N` are enforced directly by `explore()`'s own loop condition and depth check,
    deliberately never delegated here. An earlier version of this class also held
    `schedule_cap_n` and folded both conditions into one `exceeded()` boolean — caught during
    P13's own build as an honesty bug: `explore()` used that single boolean for
    `ExplorationResult.budget_exceeded`, so reaching the schedule cap alone set
    `budget_exceeded=True` too, which made `report.format_report` print "the time budget was
    exhausted" even on a run that never came close to its wall-clock deadline. Splitting the
    two signals at the source — this class answers only "has the wall clock run out?" —
    is what makes Design Constraint 5 ("be explicit about *which* bound stopped the search")
    actually true rather than merely asserted in a docstring.

    Guarantees: `deadline_wall_ms` is fixed once, at `start()`, from the sanctioned
    `agentdx.wall_time()` accessor (AGENTS.md §4.1 clause 3 covers this: exploration
    *orchestration* launches many independent runs and is not itself executing inside one —
    the same footing as `cli/`'s progress output. This value is exploration bookkeeping,
    never written into any individual run's own event log.
    """

    deadline_wall_ms: int

    @classmethod
    def start(cls, *, time_budget_s: float) -> Budget:
        """Return a fresh budget whose wall-clock deadline starts counting now."""
        return cls(deadline_wall_ms=wall_time() + int(time_budget_s * 1000))

    def exceeded(self) -> bool:
        """Return whether the wall-clock deadline has been reached.

        The schedule cap is `explore()`'s own concern, never this class's.
        """
        return wall_time() >= self.deadline_wall_ms


@dataclass(frozen=True, slots=True)
class ExploredSchedule:
    """One executed delay schedule, in the order `explore()` ran it."""

    delay_schedule: DelaySchedule
    events: tuple[Event, ...]
    turns: tuple[Turn, ...]


@dataclass(frozen=True, slots=True)
class ExplorationResult:
    """The raw, un-formatted output of `explore()`.

    `report.py` turns this into PRD §15.6's `Report`, which adds the mandatory coverage
    statement and the per-finding narrative.

    Guarantees: `unique_signatures >= len(results)` always — some discovered schedules may
    remain in the frontier, never executed, if `N` or the wall-clock deadline cut exploration
    short (`budget_exceeded`/`capped` say which). `pruned_by_reduction + duplicate_children`
    is the DEFINITION OF DONE's "pruned vs duplicate" split: the former never reached
    `dedup.SeenSchedules` at all (reduction ruled the branch point uninteresting before a
    single child was even constructed); the latter reached it and were discarded there.
    """

    delay_bound_k: int
    schedule_cap_n: int
    results: tuple[ExploredSchedule, ...]
    unique_signatures: int
    pruned_by_reduction: int
    duplicate_children: int
    capped: bool
    budget_exceeded: bool


def explore(
    execute: ScheduleExecutor,
    *,
    delay_bound_k: int,
    schedule_cap_n: int,
    time_budget_s: float,
) -> ExplorationResult:
    """Run PRD §15.3's bounded schedule exploration to completion or to a termination bound.

    Guarantees: **termination**, always (PRD §15.5) — `delay_bound_k` bounds how deep a
    schedule can grow (a child is only generated when `len(ds) < delay_bound_k`, so the
    frontier cannot grow forever even on a pathological graph), `schedule_cap_n` bounds how
    many schedules are ever executed, `time_budget_s` bounds wall-clock time; the loop checks
    all three, not just the union any one implies. **Determinism** (Design Constraint 4): this
    function performs no randomness and no unordered iteration of its own — the frontier is a
    `deque` appended in a fixed order (`reduce.interesting_steps`' own `sched_step`-ascending
    order, then `alt` ascending within a step), so the same `execute` (itself deterministic per
    its own contract) explores the same schedules in the same order on every call.

    Args:
        execute: Runs one delay schedule to completion (PRD §15.3's `execute(...)`).
        delay_bound_k: PRD §15.1 `k` — maximum scheduling points a child schedule may deviate
            at. The caller (`report.py`'s caller, ultimately `agentdx.toml` `[explore]`) is
            responsible for range-checking this against the PRD §15.1 table (0-5); this
            function accepts any non-negative int and simply explores less at k=0.
        schedule_cap_n: PRD §15.1 `N` — hard cap on schedules executed.
        time_budget_s: PRD §15.1 `time_budget_s` — wall-clock ceiling.

    Returns:
        Every executed schedule plus the accounting `report.py` needs.
    """
    seen = SeenSchedules()
    default: DelaySchedule = {}
    seen.mark(default)
    frontier: deque[DelaySchedule] = deque([default])
    results: list[ExploredSchedule] = []
    pruned_by_reduction = 0
    duplicate_children = 0
    budget = Budget.start(time_budget_s=time_budget_s)
    budget_exceeded = False

    while frontier:
        # Checked as two separate conditions, deliberately never folded into one boolean
        # (see `Budget`'s own docstring for the honesty bug that came from doing exactly
        # that): the cap check comes first, so a tie between the two is reported as
        # `capped`, never as `budget_exceeded` — a deterministic, stated priority, not an
        # accident of evaluation order.
        if len(results) >= schedule_cap_n:
            break
        if budget.exceeded():
            budget_exceeded = True
            break
        delay_schedule = frontier.popleft()
        events = execute(delay_schedule)
        turns = turns_from_events(events)
        results.append(ExploredSchedule(delay_schedule=delay_schedule, events=events, turns=turns))

        if len(delay_schedule) >= delay_bound_k:
            continue  # depth bound: this schedule generates no children

        # frozenset, not set() — pure membership testing below, never iterated (AGENTS.md
        # §4.1: check_determinism_hygiene.py bans bare `set()`; frozenset is the codebase's
        # own precedent for a hash-keyed membership check, e.g. analysis/race.py's
        # `frozenset({conflict.access_a.slot, conflict.access_b.slot})`).
        interesting = frozenset(interesting_steps(turns))
        for turn in turns:
            if turn.choices_at <= 1:
                continue  # guard 1: no choice existed, nothing to branch on
            if turn.sched_step not in interesting:
                # guard 2/3 proved every candidate independent: the choices_at - 1
                # alternatives this point would have generated are provably equivalent,
                # never constructed at all.
                pruned_by_reduction += turn.choices_at - 1
                continue
            for alt in range(1, turn.choices_at):
                # `decision_step`, never `sched_step` — see `schedule.DelaySchedule`'s
                # docstring: the `_choose()` call for this turn ran one step earlier than
                # the `sched_step` stamped on its own `schedule_decision` event.
                child: DelaySchedule = {**delay_schedule, turn.decision_step: alt}
                if seen.mark(child):
                    frontier.append(child)
                else:
                    duplicate_children += 1

    return ExplorationResult(
        delay_bound_k=delay_bound_k,
        schedule_cap_n=schedule_cap_n,
        results=tuple(results),
        unique_signatures=len(seen),
        pruned_by_reduction=pruned_by_reduction,
        duplicate_children=duplicate_children,
        # `capped` means *truncated* by the cap — reaching exactly N schedules at the same
        # moment the frontier also happens to empty out is not a truncation (nothing was
        # left to explore), so `frontier` must still be non-empty for this to be honest.
        capped=len(results) >= schedule_cap_n and bool(frontier),
        budget_exceeded=budget_exceeded,
    )


__all__ = ["Budget", "ExplorationResult", "ExploredSchedule", "ScheduleExecutor", "explore"]
