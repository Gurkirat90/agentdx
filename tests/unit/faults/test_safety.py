"""Unit tests for `runtime.faults.safety` — PRD §13.4 re-check, §13.5 hypothesis, §13.6 guards."""

from __future__ import annotations

import pytest

from agentdx.runtime.faults.registry import ArmedFault, BlastRadius, FaultDecl, Trigger
from agentdx.runtime.faults.safety import (
    AbortGuardMonitor,
    AbortGuardTripped,
    AbortPrecondition,
    ChaosAuthorizationError,
    MalformedGuardError,
    SteadyStateHypothesis,
    reauthorize,
)
from agentdx.scenario.schema import TargetKind, TriggerKind


def _armed_agent_crash(target: str) -> ArmedFault:
    decl = FaultDecl(
        fault_id="f_00",
        fault_type="agent_crash",
        target_kind=TargetKind.AGENT,
        target=target,
        trigger=Trigger(kind=TriggerKind.AT_VIRTUAL_TS, value=1000),
        params={},
    )
    return ArmedFault(decl=decl)


# ---------------------------------------------------------------------------------------
# reauthorize (PRD §13.4 point 2)
# ---------------------------------------------------------------------------------------


def test_reauthorize_passes_silently_for_a_target_inside_the_blast_radius() -> None:
    armed = _armed_agent_crash("reviewer")
    radius = BlastRadius(agents=frozenset({"reviewer"}))
    reauthorize(armed, radius)  # must not raise


def test_reauthorize_raises_e_chaos_001_for_a_target_outside_the_blast_radius() -> None:
    armed = _armed_agent_crash("reviewer")
    radius = BlastRadius(agents=frozenset({"planner"}))  # "reviewer" not included
    with pytest.raises(ChaosAuthorizationError) as excinfo:
        reauthorize(armed, radius)
    assert "E-CHAOS-001" in str(excinfo.value)
    assert "reviewer" in str(excinfo.value)


def test_reauthorize_passes_for_universal_blast_radius() -> None:
    armed = _armed_agent_crash("anyone")
    reauthorize(armed, BlastRadius(universal=True))


# ---------------------------------------------------------------------------------------
# SteadyStateHypothesis (PRD §13.5)
# ---------------------------------------------------------------------------------------


def test_hypothesis_with_no_declared_metrics_never_violates() -> None:
    hyp = SteadyStateHypothesis()
    hyp.check({}, phase="baseline")  # must not raise


def test_hypothesis_check_raises_abort_precondition_on_baseline_violation() -> None:
    hyp = SteadyStateHypothesis(task_success=">= 0.9")
    with pytest.raises(AbortPrecondition) as excinfo:
        hyp.check({"task_success": 0.5}, phase="baseline")
    assert "baseline" in str(excinfo.value)
    assert "task_success" in str(excinfo.value)


def test_hypothesis_check_passes_when_metric_satisfies_comparison() -> None:
    hyp = SteadyStateHypothesis(task_success=">= 0.9")
    hyp.check({"task_success": 0.95}, phase="baseline")  # must not raise


def test_hypothesis_violation_when_declared_metric_missing_from_measurements() -> None:
    hyp = SteadyStateHypothesis(p95_virtual_duration_ms="<= 5000")
    violations = hyp.violations({})
    assert len(violations) == 1
    assert "not measured" in violations[0]


def test_hypothesis_from_resolved_scenario_reads_the_hypothesis_section() -> None:
    resolved = {"hypothesis": {"task_success": ">= 0.9", "max_token_spend": "<= 10000"}}
    hyp = SteadyStateHypothesis.from_resolved_scenario(resolved)
    assert hyp.task_success == ">= 0.9"
    assert hyp.max_token_spend == "<= 10000"  # noqa: S105 — a comparison string, not a secret
    assert hyp.p95_virtual_duration_ms is None


# ---------------------------------------------------------------------------------------
# AbortGuardMonitor (PRD §13.6)
# ---------------------------------------------------------------------------------------


def _monitor(**overrides: int) -> AbortGuardMonitor:
    base = {
        "max_virtual_duration_ms": 120_000,
        "max_tokens": 200_000,
        "max_retries": 20,
        "max_wall_duration_s": 300,
        "max_events": 500_000,
        "max_llm_calls": 500,
    }
    base.update(overrides)
    return AbortGuardMonitor(**base)


def test_observe_step_trips_max_virtual_duration_ms() -> None:
    monitor = _monitor(max_virtual_duration_ms=1000)
    assert monitor.observe_step(step=0, virtual_ts_ms=500, wall_elapsed_ms=0) is None
    trip = monitor.observe_step(step=1, virtual_ts_ms=1001, wall_elapsed_ms=0)
    assert trip is not None
    assert trip.guard == "max_virtual_duration_ms"
    assert "E-GUARD-001" in str(trip)


def test_observe_step_trips_max_wall_duration_s_only_every_100_steps() -> None:
    monitor = _monitor(max_wall_duration_s=1)
    # Step 50 is not a multiple of 100 — the wall-duration check is skipped even though the
    # wall budget is already exceeded (PRD §13.6: "Evaluated ... Every 100 steps").
    assert monitor.observe_step(step=50, virtual_ts_ms=0, wall_elapsed_ms=5000) is None
    trip = monitor.observe_step(step=100, virtual_ts_ms=0, wall_elapsed_ms=5000)
    assert trip is not None
    assert trip.guard == "max_wall_duration_s"


def test_observe_llm_call_trips_max_llm_calls() -> None:
    monitor = _monitor(max_llm_calls=2)
    assert monitor.observe_llm_call(prompt_tokens=1, completion_tokens=1) is None
    trip = monitor.observe_llm_call(prompt_tokens=1, completion_tokens=1)
    assert trip is None  # exactly at the budget, not yet over
    trip = monitor.observe_llm_call(prompt_tokens=1, completion_tokens=1)
    assert trip is not None
    assert trip.guard == "max_llm_calls"


def test_observe_llm_call_trips_max_tokens() -> None:
    monitor = _monitor(max_tokens=100)
    trip = monitor.observe_llm_call(prompt_tokens=60, completion_tokens=50)
    assert trip is not None
    assert trip.guard == "max_tokens"


def test_observe_retry_trips_max_retries() -> None:
    monitor = _monitor(max_retries=1)
    assert monitor.observe_retry() is None
    trip = monitor.observe_retry()
    assert trip is not None
    assert trip.guard == "max_retries"


def test_observe_event_batch_trips_max_events() -> None:
    monitor = _monitor(max_events=10)
    assert monitor.observe_event_batch(5) is None
    trip = monitor.observe_event_batch(6)
    assert trip is not None
    assert trip.guard == "max_events"


def test_from_resolved_guards_reads_every_field() -> None:
    guards = {
        "max_virtual_duration_ms": 1,
        "max_tokens": 2,
        "max_retries": 3,
        "max_wall_duration_s": 4,
        "max_events": 5,
        "max_llm_calls": 6,
    }
    monitor = AbortGuardMonitor.from_resolved_guards(guards)
    assert monitor.max_virtual_duration_ms == 1
    assert monitor.max_llm_calls == 6


def test_from_resolved_guards_rejects_non_int_value() -> None:
    guards: dict[str, object] = {
        "max_virtual_duration_ms": "not-an-int",
        "max_tokens": 2,
        "max_retries": 3,
        "max_wall_duration_s": 4,
        "max_events": 5,
        "max_llm_calls": 6,
    }
    with pytest.raises(MalformedGuardError):
        AbortGuardMonitor.from_resolved_guards(guards)


def test_abort_guard_tripped_error_message_includes_trip_detail() -> None:
    monitor = _monitor(max_retries=0)
    trip = monitor.observe_retry()
    assert trip is not None
    err = AbortGuardTripped(trip)
    assert "E-GUARD-001" in str(err)
    assert err.trip is trip
