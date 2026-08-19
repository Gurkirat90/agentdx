"""Integration tests proving P13's DEFINITION OF DONE against the real Scheduler harness.

Each test corresponds to one literal DoD line item. `_harness.py` explains why these run
against a hand-built, `research_fanout`/`code_pipeline`-*shaped* scenario rather than the
fixtures themselves — a signed-off, out-of-scope-wiring-gap workaround (see its docstring
and `docs/exploration.md`), not a shortcut around the requirement itself.
"""

from __future__ import annotations

from agentdx.analysis.race import detect_conflicts
from agentdx.explore.generate import explore
from agentdx.explore.reduce import interesting_steps
from agentdx.explore.report import COVERAGE_STATEMENT, build_report, format_report
from agentdx.explore.schedule import (
    DelaySchedule,
    canonical_delay_schedule_text,
    signature,
    turns_from_events,
)
from tests.integration.explore._harness import (
    ScheduleExecutor,
    make_code_pipeline_executor,
    make_research_fanout_executor,
)


def _reference_frontier(execute: ScheduleExecutor, *, delay_bound_k: int) -> frozenset[str]:
    """Independently enumerate every unique delay-schedule signature reachable at `k`.

    OP-3 follow-up to a real self-audit finding against P13 (2026-08-19): the original G2
    frontier test only asserted "more than one schedule executed" and "zero findings on
    whichever schedules ran" — a bug in `generate.explore()`'s own BFS orchestration that
    over-aggressively marked legitimate, distinct schedules as duplicates would silently
    shrink the frontier without failing either assertion. This function is a *depth-first*
    reference traversal, structurally different from `explore()`'s breadth-first loop, but
    built from the same lower-level, separately-unit-tested primitives (`reduce.
    interesting_steps`, `schedule.Turn.decision_step`, `schedule.signature`) — never calling
    `generate.explore()` itself. A bug specific to `explore()`'s own frontier bookkeeping
    would show up as a mismatch between this function's result and `explore()`'s; a bug
    shared by both because it lives inside `reduce.py`/`dedup.py` is out of this
    cross-check's scope (`test_reduce.py`/`test_dedup.py` already cover those directly).
    """
    seen: set[str] = set()

    def visit(delay_schedule: DelaySchedule) -> None:
        sig = signature(delay_schedule)
        if sig in seen:
            return
        seen.add(sig)
        events = execute(delay_schedule)
        turns = turns_from_events(events)
        if len(delay_schedule) >= delay_bound_k:
            return
        interesting = frozenset(interesting_steps(turns))
        for turn in turns:
            if turn.choices_at <= 1 or turn.sched_step not in interesting:
                continue
            for alt in range(1, turn.choices_at):
                child: DelaySchedule = {**delay_schedule, turn.decision_step: alt}
                visit(child)

    visit({})
    return frozenset(seen)


def test_code_pipeline_shaped_k2_n200_completes_with_verbatim_coverage_statement() -> None:
    """DoD item 1: k=2/N=200 completes on the code-pipeline-shaped scenario.

    The report's rendered text carries the I10 coverage statement byte-for-byte.
    """
    result = explore(
        make_code_pipeline_executor(), delay_bound_k=2, schedule_cap_n=200, time_budget_s=60.0
    )
    report = build_report(result)
    text = format_report(report)

    assert result.budget_exceeded is False  # completed, not truncated by the clock
    assert COVERAGE_STATEMENT in text
    assert COVERAGE_STATEMENT == report.coverage_statement
    # A genuine, unsynchronised write_write race is present -- the exploration engine's
    # report pipeline (`report.build_report`) actually surfaces what P12's detector finds,
    # it does not merely thread the statement through past an empty result.
    assert len(report.new_findings) >= 1
    assert any(f.finding.subtype == "write_write" for f in report.new_findings)


def test_research_fanout_shaped_g2_holds_across_entire_k2_frontier() -> None:
    """DoD item 2: research-fanout-shaped G2 holds across the WHOLE k=2 frontier.

    Not just the default schedule -- every single executed interleaving in the frontier
    must independently show zero findings, checked one schedule at a time. "The whole
    frontier" is itself cross-checked against an independent traversal (below) rather than
    trusted as "however many schedules explore() happened to run" -- an OP-3 follow-up to
    a real self-audit finding: a silent-truncation bug would have let the original,
    weaker version of this test pass by shrinking the very frontier it claims to cover.
    """
    executor = make_research_fanout_executor()
    result = explore(executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=60.0)
    assert result.budget_exceeded is False
    assert len(result.results) > 1  # the frontier is genuinely more than just the default

    explored_signatures = frozenset(signature(r.delay_schedule) for r in result.results)
    reference_signatures = _reference_frontier(executor, delay_bound_k=2)
    assert explored_signatures == reference_signatures, (
        f"explore()'s own BFS frontier ({len(explored_signatures)} schedules) does not "
        f"match an independently-structured DFS traversal over the same reduction/dedup "
        f"primitives ({len(reference_signatures)} schedules) -- possible silent frontier "
        f"truncation inside explore()'s own orchestration"
    )

    per_schedule_counts = [len(detect_conflicts(r.events)) for r in result.results]
    assert all(count == 0 for count in per_schedule_counts), (
        f"G2 FAILURE: a finding appeared on at least one schedule in the k=2 frontier "
        f"({per_schedule_counts})"
    )

    report = build_report(result)
    assert report.new_findings == ()


def test_same_seed_identical_explored_schedule_sequence_20_of_20() -> None:
    """DoD item 3: same root/seed -> same schedules in the same order, 20 times running."""
    sequences: list[tuple[str, ...]] = []
    for _ in range(20):
        result = explore(
            make_code_pipeline_executor(),
            delay_bound_k=2,
            schedule_cap_n=200,
            time_budget_s=60.0,
        )
        sequences.append(
            tuple(canonical_delay_schedule_text(r.delay_schedule) for r in result.results)
        )

    first = sequences[0]
    matches = sum(1 for seq in sequences if seq == first)
    assert matches == 20, f"only {matches}/20 runs matched the first run's schedule sequence"
    assert len(first) > 1  # non-trivial: more than just the default was explored


def test_reduction_effectiveness_measured_and_reported() -> None:
    """DoD item 4: reduction effectiveness (explored/pruned/duplicate) is measured, not assumed.

    `research_fanout`-shaped is the harness where reduction should have *something* to
    measure (three writers, `interesting_steps`' fail-open only stops firing once a
    candidate has taken an earlier turn -- with three concurrent workers that happens
    before the frontier is exhausted).
    """
    result = explore(
        make_research_fanout_executor(), delay_bound_k=2, schedule_cap_n=200, time_budget_s=60.0
    )
    report = build_report(result)

    # All three counts are actual measurements, not fixed constants -- assert they are
    # well-formed and internally consistent rather than pinning exact numbers here (that
    # is `test_schedule.py`/`test_generate.py`'s job against a hand-built fake executor).
    assert report.schedules_executed == len(result.results)
    assert report.unique_schedules >= report.schedules_executed
    assert report.reduced_away >= 0
    assert report.duplicate_children >= 0
    # Reduction is doing *something* on this shape -- not a hard requirement of explore()
    # itself, but a sanity check that this particular harness is worth using as evidence.
    assert report.reduced_away > 0

    text = format_report(report)
    assert "reduced away" in text
    assert "duplicate schedules" in text
    assert "schedules executed" in text
