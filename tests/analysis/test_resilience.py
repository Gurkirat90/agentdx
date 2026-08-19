"""PRD §19 — per-fault and aggregate resilience scoring, over hand-authored event logs.

Unlike `test_verdict.py`, this module's `score()` genuinely reads raw events (§19's inputs —
`success_check` assertions, `fault_injected`/`fault_id` taint, `retry_of` attributes — all live
only in the log), so the fixtures here are small hand-authored `Event` sequences via `tests.
analysis._events`'s builders, not directly-constructed dataclasses.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentdx.analysis.resilience import (
    DegradationClass,
    FaultRunInput,
    FaultRunStatus,
    ResilienceAnalysisError,
    classify_degradation,
    format_resilience_table,
    load_resilience_rules,
    score,
)
from agentdx.events.schema import Event
from tests.analysis._events import (
    assertion_result,
    fault_injected,
    run_end,
    run_start,
    span_end,
    span_start,
)

_RULES = load_resilience_rules()


# ---------------------------------------------------------------------------------------------
# classify_degradation — DEGRADED_FLAGGED is a declared, tested, structural gap
# ---------------------------------------------------------------------------------------------


def test_degraded_flagged_is_never_produced_by_this_build() -> None:
    """The module docstring's "Degradation classification, ruled" note, demonstrated.

    `DEGRADED_FLAGGED` is declared on the enum (for a future PRD-amendment-driven classifier
    upgrade) but structurally unreachable from this build's `classify_degradation` — swept
    across every input combination the function accepts, not just a couple of examples.
    """
    for success_check_passed in (True, False):
        for run_end_status in ("complete", "failed", "aborted", "timeout", "aborted_guard"):
            result = classify_degradation(
                success_check_passed=success_check_passed, run_end_status=run_end_status
            )
            assert result is not DegradationClass.DEGRADED_FLAGGED


def test_classify_degradation_graceful_when_passed() -> None:
    assert (
        classify_degradation(success_check_passed=True, run_end_status="complete")
        is DegradationClass.GRACEFUL
    )
    assert (
        classify_degradation(success_check_passed=True, run_end_status="failed")
        is DegradationClass.GRACEFUL
    )


def test_classify_degradation_silent_failure_when_system_claims_success_but_check_failed() -> None:
    """PRD §19.5's own definition, verbatim: reported success while success_check failed."""
    assert (
        classify_degradation(success_check_passed=False, run_end_status="complete")
        is DegradationClass.SILENT_FAILURE
    )


def test_classify_degradation_hard_failure_when_system_also_reports_non_success() -> None:
    assert (
        classify_degradation(success_check_passed=False, run_end_status="failed")
        is DegradationClass.HARD_FAILURE
    )


# ---------------------------------------------------------------------------------------------
# Fixtures — a no-fault baseline run, and per-fault run logs
# ---------------------------------------------------------------------------------------------


def _baseline_events(*, makespan_ms: int = 100) -> tuple[Event, ...]:
    return (
        run_start(seq=0, virtual_ts_ms=0),
        assertion_result(
            seq=1,
            virtual_ts_ms=makespan_ms,
            vclock={"_run": 1},
            causal_parents=[0],
            assertion_id="a1",
            kind="success_check",
            passed=True,
        ),
        run_end(
            seq=2,
            virtual_ts_ms=makespan_ms,
            vclock={"_run": 2},
            causal_parents=[1],
            virtual_makespan_ms=makespan_ms,
            event_count=3,
        ),
    )


def _fault_run_events(
    *,
    fault_id: str = "f1",
    recovered_at_ms: int | None = 20,
    success_check_passed: bool = True,
    run_end_status: str = "complete",
    n_retries: int = 0,
    makespan_ms: int = 100,
) -> tuple[Event, ...]:
    events: list[Event] = [
        run_start(seq=0, virtual_ts_ms=0),
        fault_injected(
            seq=1, virtual_ts_ms=5, vclock={"_run": 1}, causal_parents=[0], fault_id=fault_id
        ),
    ]
    seq = 2
    for i in range(n_retries):
        events.append(
            span_start(
                seq=seq,
                virtual_ts_ms=6 + i,
                vclock={"a": seq},
                causal_parents=[1],
                agent_id="a",
                span_id=f"retry{i}",
                kind="tool_call",
                name="retry_attempt",
                attributes={"retry_of": "orig_span"},
            )
        )
        seq += 1
    if recovered_at_ms is not None:
        # The recovering span: span_start (fault-tainted via the taint mechanism, represented
        # here by directly setting Event.fault_id on the span_end since `_events.span_end` has
        # no fault_id parameter) then span_end carrying that fault's id and status "ok".
        recovering_end = span_end(
            seq=seq,
            virtual_ts_ms=recovered_at_ms,
            vclock={"a": seq},
            causal_parents=[1],
            agent_id="a",
            span_id="recovering",
            duration_virtual_ms=recovered_at_ms - 5,
        )
        events.append(replace(recovering_end, fault_id=fault_id))
        seq += 1
    events.append(
        assertion_result(
            seq=seq,
            virtual_ts_ms=makespan_ms,
            vclock={"_run": seq},
            causal_parents=[seq - 1],
            assertion_id="a1",
            kind="success_check",
            passed=success_check_passed,
        )
    )
    seq += 1
    events.append(
        run_end(
            seq=seq,
            virtual_ts_ms=makespan_ms,
            vclock={"_run": seq},
            causal_parents=[seq - 1],
            virtual_makespan_ms=makespan_ms,
            event_count=seq + 1,
            status=run_end_status,
        )
    )
    return tuple(events)


# ---------------------------------------------------------------------------------------------
# score() — per-fault scoring, exclusion rules, and the hard silent-failure cap
# ---------------------------------------------------------------------------------------------


def test_score_a_clean_recovery_scores_high() -> None:
    baseline = _baseline_events()
    fault_run = FaultRunInput(
        fault_id="f1", fault_label="latency(agent_a)", events=_fault_run_events(recovered_at_ms=10)
    )
    result = score(baseline, [fault_run])

    assert result.n_faults == 1
    assert result.not_fired == ()
    assert result.aborted == ()
    fault_score = result.per_fault[0]
    assert fault_score.status is FaultRunStatus.SCORED
    assert fault_score.success_ratio == 1.0
    assert fault_score.degradation_class is DegradationClass.GRACEFUL
    assert fault_score.recovery_time_virtual_ms == 5  # 10 - 5 (fault_injected's own vts)
    assert fault_score.score is not None
    assert result.resilience_score is not None
    assert result.resilience_score == round(fault_score.score)
    assert not result.silent_failure_capped
    assert fault_score.evidence_seq  # I6-style: a real number always traces to real seqs


def test_score_never_recovering_scores_zero_recovery_component() -> None:
    baseline = _baseline_events()
    fault_run = FaultRunInput(
        fault_id="f1", fault_label="crash(agent_a)", events=_fault_run_events(recovered_at_ms=None)
    )
    result = score(baseline, [fault_run])
    fault_score = result.per_fault[0]
    assert fault_score.recovery_time_virtual_ms is None
    assert fault_score.recovery_component == 0.0


def test_a_single_silent_failure_caps_the_aggregate_at_49() -> None:
    """Design Constraint 6 / the Definition of Done's explicit test.

    PRD §19.7.4, non-negotiable: any `silent_failure` present hard-caps the aggregate at 49,
    *even when* the weighted mean of the per-fault scores alone would be much higher.
    """
    baseline = _baseline_events()
    good_fault = FaultRunInput(
        fault_id="f1",
        fault_label="latency(a)",
        events=_fault_run_events(fault_id="f1", recovered_at_ms=6),
    )
    silent_failure_fault = FaultRunInput(
        fault_id="f2",
        fault_label="crash(b)",
        events=_fault_run_events(
            fault_id="f2", success_check_passed=False, run_end_status="complete"
        ),
    )
    result = score(baseline, [good_fault, silent_failure_fault])

    silent = next(f for f in result.per_fault if f.fault_id == "f2")
    assert silent.degradation_class is DegradationClass.SILENT_FAILURE
    assert result.silent_failure_capped
    assert result.resilience_score is not None
    assert result.resilience_score <= _RULES.silent_failure_cap
    assert result.resilience_score <= 49


def test_score_no_silent_failure_does_not_cap() -> None:
    baseline = _baseline_events()
    fault_run = FaultRunInput(
        fault_id="f1", fault_label="latency(a)", events=_fault_run_events(recovered_at_ms=6)
    )
    result = score(baseline, [fault_run])
    assert not result.silent_failure_capped


def test_score_excludes_a_fault_that_never_fired() -> None:
    baseline = _baseline_events()
    # No fault_injected event for "f1" anywhere in this log.
    events = (
        run_start(seq=0, virtual_ts_ms=0),
        assertion_result(
            seq=1,
            virtual_ts_ms=100,
            vclock={"_run": 1},
            causal_parents=[0],
            assertion_id="a1",
            kind="success_check",
            passed=True,
        ),
        run_end(
            seq=2,
            virtual_ts_ms=100,
            vclock={"_run": 2},
            causal_parents=[1],
            virtual_makespan_ms=100,
            event_count=3,
        ),
    )
    fault_run = FaultRunInput(fault_id="f1", fault_label="never_fires(a)", events=events)
    result = score(baseline, [fault_run])

    assert result.not_fired == ("f1",)
    assert result.n_faults == 0
    assert result.resilience_score is None  # never fabricated as 0 or 100
    fault_score = result.per_fault[0]
    assert fault_score.status is FaultRunStatus.NOT_FIRED
    assert fault_score.score is None
    assert fault_score.evidence_seq == ()


def test_score_excludes_an_aborted_guard_run() -> None:
    baseline = _baseline_events()
    events = _fault_run_events(run_end_status="aborted_guard")
    fault_run = FaultRunInput(fault_id="f1", fault_label="aborted(a)", events=events)
    result = score(baseline, [fault_run])

    assert result.aborted == ("f1",)
    assert result.n_faults == 0
    fault_score = result.per_fault[0]
    assert fault_score.status is FaultRunStatus.ABORTED
    assert fault_score.score is None


def test_score_mixes_scored_not_fired_and_aborted_correctly() -> None:
    baseline = _baseline_events()
    scored = FaultRunInput(
        fault_id="f1",
        fault_label="ok(a)",
        events=_fault_run_events(fault_id="f1", recovered_at_ms=6),
    )
    not_fired_events = (
        run_start(seq=0, virtual_ts_ms=0),
        run_end(
            seq=1,
            virtual_ts_ms=10,
            vclock={"_run": 1},
            causal_parents=[0],
            virtual_makespan_ms=10,
            event_count=2,
        ),
    )
    not_fired = FaultRunInput(fault_id="f2", fault_label="never(b)", events=not_fired_events)
    aborted = FaultRunInput(
        fault_id="f3",
        fault_label="aborted(c)",
        events=_fault_run_events(fault_id="f3", run_end_status="aborted_guard"),
    )
    result = score(baseline, [scored, not_fired, aborted])

    assert result.n_faults == 1
    assert result.not_fired == ("f2",)
    assert result.aborted == ("f3",)
    assert len(result.per_fault) == 3  # every input still gets a row (§19.7.1)


def test_score_retry_amplification_reduces_the_amplification_component() -> None:
    baseline = _baseline_events()
    no_retries = FaultRunInput(
        fault_id="f1", fault_label="clean(a)", events=_fault_run_events(fault_id="f1", n_retries=0)
    )
    many_retries = FaultRunInput(
        fault_id="f1",
        fault_label="thrashing(a)",
        events=_fault_run_events(fault_id="f1", n_retries=8),
    )
    calm = score(baseline, [no_retries]).per_fault[0]
    thrashing = score(baseline, [many_retries]).per_fault[0]

    assert calm.amplification == 0.0  # 0 fault retries / max(0 baseline retries, 1)
    assert thrashing.amplification == 8.0
    assert calm.amplification_component is not None
    assert thrashing.amplification_component is not None
    assert thrashing.amplification_component < calm.amplification_component


def test_score_raises_e_res_001_when_baseline_has_no_success_check() -> None:
    baseline_no_check = (
        run_start(seq=0, virtual_ts_ms=0),
        run_end(
            seq=1,
            virtual_ts_ms=10,
            vclock={"_run": 1},
            causal_parents=[0],
            virtual_makespan_ms=10,
            event_count=2,
        ),
    )
    fault_run = FaultRunInput(fault_id="f1", fault_label="a", events=_fault_run_events())
    with pytest.raises(ResilienceAnalysisError) as excinfo:
        score(baseline_no_check, [fault_run])
    assert excinfo.value.code == "E-RES-001"


def test_score_raises_e_res_001_when_a_fired_fault_run_has_no_success_check() -> None:
    baseline = _baseline_events()
    events_missing_check = (
        run_start(seq=0, virtual_ts_ms=0),
        fault_injected(
            seq=1, virtual_ts_ms=5, vclock={"_run": 1}, causal_parents=[0], fault_id="f1"
        ),
        run_end(
            seq=2,
            virtual_ts_ms=10,
            vclock={"_run": 2},
            causal_parents=[1],
            virtual_makespan_ms=10,
            event_count=3,
        ),
    )
    fault_run = FaultRunInput(fault_id="f1", fault_label="a", events=events_missing_check)
    with pytest.raises(ResilienceAnalysisError) as excinfo:
        score(baseline, [fault_run])
    assert excinfo.value.code == "E-RES-001"


def test_score_weighted_mean_respects_fault_weights() -> None:
    baseline = _baseline_events()
    good = FaultRunInput(
        fault_id="f1",
        fault_label="good(a)",
        events=_fault_run_events(fault_id="f1", recovered_at_ms=6),
        weight=1.0,
    )
    bad = FaultRunInput(
        fault_id="f2",
        fault_label="bad(b)",
        events=_fault_run_events(fault_id="f2", recovered_at_ms=None, n_retries=8),
        weight=1.0,
    )
    equal_weighted = score(baseline, [good, bad])
    heavily_favours_good = score(baseline, [good, bad], fault_weights={"f1": 100.0, "f2": 1.0})

    assert heavily_favours_good.resilience_score is not None
    assert equal_weighted.resilience_score is not None
    assert heavily_favours_good.resilience_score >= equal_weighted.resilience_score


def test_score_empty_fault_runs_yields_none_not_a_fabricated_number() -> None:
    baseline = _baseline_events()
    result = score(baseline, [])
    assert result.resilience_score is None
    assert result.worst_fault_score is None
    assert result.n_faults == 0
    assert result.per_fault == ()


# ---------------------------------------------------------------------------------------------
# format_resilience_table — PRD §19.8's shape, and load_resilience_rules/round-trip
# ---------------------------------------------------------------------------------------------


def test_format_resilience_table_prints_every_fault_row_and_the_cap_warning() -> None:
    baseline = _baseline_events()
    good = FaultRunInput(
        fault_id="f1",
        fault_label="latency(a)",
        events=_fault_run_events(fault_id="f1", recovered_at_ms=6),
    )
    silent = FaultRunInput(
        fault_id="f2",
        fault_label="crash(b)",
        events=_fault_run_events(
            fault_id="f2", success_check_passed=False, run_end_status="complete"
        ),
    )
    result = score(baseline, [good, silent])
    text = format_resilience_table(result)

    assert "Resilience:" in text
    assert "latency(a)" in text
    assert "crash(b)" in text
    assert "not fired: none" in text
    assert "aborted: none" in text
    assert "silent_failure" in text  # the cap warning line
    print("\n" + text)  # noqa: T201


def test_load_resilience_rules_matches_the_committed_toml() -> None:
    rules = load_resilience_rules()
    assert rules.silent_failure_cap == 49
    assert rules.recovery_budget_multiplier == pytest.approx(2.0)
    assert rules.amplification_budget == pytest.approx(4.0)
    assert rules.success_ratio_weight == pytest.approx(0.50)
    assert rules.recovery_weight == pytest.approx(0.20)
    assert rules.amplification_weight == pytest.approx(0.15)
    assert rules.degradation_weight == pytest.approx(0.15)
    assert rules.degradation_weights[DegradationClass.GRACEFUL] == pytest.approx(1.0)
    assert rules.degradation_weights[DegradationClass.SILENT_FAILURE] == pytest.approx(0.0)
