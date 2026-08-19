"""Unit tests for `explore/report.py` — the I10 honesty requirement (AGENTS.md §5)."""

from __future__ import annotations

import pytest

from agentdx.explore.generate import ExplorationResult, ExploredSchedule
from agentdx.explore.report import (
    COVERAGE_STATEMENT,
    Report,
    build_report,
    format_report,
    to_api_payload,
)
from tests.unit.explore._factories import schedule_decision, state_write


def _exploration_with_one_race() -> ExplorationResult:
    """One executed schedule whose log contains one genuine, unsuppressed write_write race."""
    events = (
        schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=["b"]),
        state_write(seq=1, sched_step=1, agent_id="a", key="shared", value_hash="vh_a"),
        schedule_decision(seq=2, sched_step=2, chosen_task_id="b", ready_task_ids=[]),
        state_write(seq=3, sched_step=2, agent_id="b", key="shared", value_hash="vh_b"),
    )
    from agentdx.explore.schedule import turns_from_events

    executed = ExploredSchedule(delay_schedule={}, events=events, turns=turns_from_events(events))
    return ExplorationResult(
        delay_bound_k=2,
        schedule_cap_n=200,
        results=(executed,),
        unique_signatures=1,
        pruned_by_reduction=0,
        duplicate_children=0,
        capped=False,
        budget_exceeded=False,
    )


def _exploration_with_no_findings() -> ExplorationResult:
    events = (schedule_decision(seq=0, sched_step=1, chosen_task_id="a", ready_task_ids=[]),)
    from agentdx.explore.schedule import turns_from_events

    executed = ExploredSchedule(delay_schedule={}, events=events, turns=turns_from_events(events))
    return ExplorationResult(
        delay_bound_k=2,
        schedule_cap_n=200,
        results=(executed,),
        unique_signatures=1,
        pruned_by_reduction=0,
        duplicate_children=0,
        capped=False,
        budget_exceeded=False,
    )


# ---------------------------------------------------------------------------------------
# I10 — the coverage statement itself
# ---------------------------------------------------------------------------------------


def test_coverage_statement_is_the_prd_verbatim_sentence() -> None:
    assert COVERAGE_STATEMENT == "Bounded search: absence of findings is not proof of absence."


def test_report_rejects_construction_with_wrong_coverage_statement() -> None:
    with pytest.raises(ValueError, match="coverage_statement"):
        Report(
            delay_bound_k=2,
            schedules_executed=1,
            schedule_cap_n=200,
            unique_schedules=1,
            reduced_away=0,
            duplicate_children=0,
            new_findings=(),
            capped=False,
            budget_exceeded=False,
            coverage_statement="absence of findings proves nothing was found",
        )


def test_report_default_coverage_statement_is_correct() -> None:
    report = Report(
        delay_bound_k=2,
        schedules_executed=1,
        schedule_cap_n=200,
        unique_schedules=1,
        reduced_away=0,
        duplicate_children=0,
        new_findings=(),
        capped=False,
        budget_exceeded=False,
    )
    assert report.coverage_statement == COVERAGE_STATEMENT


def test_format_report_contains_coverage_statement_verbatim() -> None:
    report = build_report(_exploration_with_no_findings())
    text = format_report(report)
    assert COVERAGE_STATEMENT in text


def test_to_api_payload_has_required_top_level_coverage_statement_key() -> None:
    report = build_report(_exploration_with_no_findings())
    payload = to_api_payload(report)
    assert payload["coverage_statement"] == COVERAGE_STATEMENT


# ---------------------------------------------------------------------------------------
# build_report — findings, counts
# ---------------------------------------------------------------------------------------


def test_build_report_surfaces_a_genuine_finding() -> None:
    report = build_report(_exploration_with_one_race())
    assert len(report.new_findings) == 1
    finding = report.new_findings[0].finding
    assert finding.subtype == "write_write"
    assert finding.key == "shared"
    assert report.new_findings[0].first_seen_at_k == 0


def test_build_report_zero_findings_stays_zero() -> None:
    report = build_report(_exploration_with_no_findings())
    assert report.new_findings == ()


def test_build_report_preserves_exploration_bookkeeping() -> None:
    exploration = _exploration_with_no_findings()
    report = build_report(exploration)
    assert report.delay_bound_k == exploration.delay_bound_k
    assert report.schedules_executed == len(exploration.results)
    assert report.schedule_cap_n == exploration.schedule_cap_n
    assert report.unique_schedules == exploration.unique_signatures
    assert report.reduced_away == exploration.pruned_by_reduction
    assert report.duplicate_children == exploration.duplicate_children
    assert report.capped == exploration.capped
    assert report.budget_exceeded == exploration.budget_exceeded


def test_format_report_notes_budget_exceeded_honestly() -> None:
    exploration = _exploration_with_no_findings()
    from dataclasses import replace

    partial = replace(exploration, budget_exceeded=True)
    report = build_report(partial)
    text = format_report(report)
    assert "partial exploration" in text
    assert "reported honestly" in text


def test_format_report_notes_capped_honestly_when_not_budget_exceeded() -> None:
    exploration = _exploration_with_no_findings()
    from dataclasses import replace

    partial = replace(exploration, capped=True)
    report = build_report(partial)
    text = format_report(report)
    assert "schedule cap" in text
    assert "reported honestly" in text


def test_to_api_payload_shape_for_a_finding() -> None:
    report = build_report(_exploration_with_one_race())
    payload = to_api_payload(report)
    assert len(payload["new_findings"]) == 1
    entry = payload["new_findings"][0]
    assert entry["subtype"] == "write_write"
    assert entry["key"] == "shared"
    assert entry["first_seen_at_k"] == 0
