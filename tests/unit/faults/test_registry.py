"""Unit tests for `runtime.faults.registry` (PRD §12.1, §13.3-13.4)."""

from __future__ import annotations

import pytest

from agentdx.runtime.faults.registry import (
    BlastRadius,
    ChaosAuthorizationError,
    FaultNotImplementedError,
    FaultRegistry,
    param_int,
    params_payload,
)
from agentdx.scenario.schema import TargetKind, TriggerKind
from tests.unit.faults.conftest import resolved_scenario


def test_arms_mvp_fault_against_fixture_default_universal_blast_radius() -> None:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 3000}]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)

    assert len(registry.faults) == 1
    armed = registry.faults[0]
    assert armed.decl.fault_id == "f_00"
    assert armed.decl.fault_type == "agent_crash"
    assert armed.decl.target_kind is TargetKind.AGENT
    assert armed.decl.target == "reviewer"
    assert armed.decl.trigger.kind is TriggerKind.AT_VIRTUAL_TS
    assert armed.decl.trigger.value == 3000
    assert registry.blast_radius.universal is True
    assert not armed.fired


def test_fault_ids_are_assigned_by_declaration_order() -> None:
    resolved = resolved_scenario(
        faults=[
            {"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 1000},
            {"type": "agent_crash", "agent": "tester", "at_virtual_ts": 2000},
        ]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    assert [f.decl.fault_id for f in registry.faults] == ["f_00", "f_01"]


def test_p1_fault_type_raises_fault_not_implemented_error() -> None:
    resolved = resolved_scenario(faults=[{"type": "message_reorder", "edge": "a->b", "window": 2}])
    with pytest.raises(FaultNotImplementedError) as excinfo:
        FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    assert "E-CHAOS-002" in str(excinfo.value)
    assert "message_reorder" in str(excinfo.value)


def test_user_graph_faults_without_chaos_opt_in_raise_authorization_error() -> None:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 1000}],
        chaos_opt_in=False,
    )
    with pytest.raises(ChaosAuthorizationError) as excinfo:
        FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    assert "E-CHAOS-001" in str(excinfo.value)


def test_user_graph_faults_with_opt_in_but_target_outside_blast_radius_raises() -> None:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 1000}],
        chaos_opt_in=True,
        blast_radius={"agents": ["planner"]},  # "reviewer" not included
    )
    with pytest.raises(ChaosAuthorizationError):
        FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)


def test_user_graph_faults_inside_declared_blast_radius_arm_cleanly() -> None:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 1000}],
        chaos_opt_in=True,
        blast_radius={"agents": ["reviewer"]},
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    assert len(registry.faults) == 1
    assert registry.blast_radius.universal is False


def test_summary_reports_fault_not_triggered_before_any_fire() -> None:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 3000}]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    summary = registry.summary()
    assert summary == (
        {
            "fault_id": "f_00",
            "fault_type": "agent_crash",
            "target": "reviewer",
            "fired_count": 0,
            "first_fired_at": None,
            "targets_affected": [],
            "fault_not_triggered": True,
        },
    )


def test_summary_after_record_fire() -> None:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 3000}]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    armed = registry.faults[0]
    armed.record_fire(virtual_ts_ms=3000, target="reviewer")
    summary = registry.summary()[0]
    assert summary["fired_count"] == 1
    assert summary["first_fired_at"] == 3000
    assert summary["targets_affected"] == ["reviewer"]
    assert summary["fault_not_triggered"] is False


def test_by_type_filters_and_preserves_order() -> None:
    resolved = resolved_scenario(
        faults=[
            {"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 1000},
            {"type": "latency", "edge": "a->b", "always": True, "delay_ms": 50},
            {"type": "agent_crash", "agent": "tester", "at_virtual_ts": 2000},
        ]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    crashes = registry.by_type("agent_crash")
    assert [f.decl.target for f in crashes] == ["reviewer", "tester"]


def test_blast_radius_state_key_glob_matching() -> None:
    radius = BlastRadius(state_keys=("draft.*",))
    assert radius.contains(TargetKind.STATE_KEY, "draft.body") is True
    assert radius.contains(TargetKind.STATE_KEY, "other.key") is False


def test_blast_radius_agent_membership_is_exact_no_globs() -> None:
    radius = BlastRadius(agents=frozenset({"reviewer"}))
    assert radius.contains(TargetKind.AGENT, "reviewer") is True
    assert radius.contains(TargetKind.AGENT, "review*") is False


def test_blast_radius_universal_short_circuits_every_kind() -> None:
    radius = BlastRadius(universal=True)
    assert radius.contains(TargetKind.AGENT, "anything") is True
    assert radius.contains(TargetKind.STATE_KEY, "anything.else") is True


def test_params_payload_copies_scalar_values() -> None:
    payload = params_payload({"recoverable": True, "restart_after_ms": 500})
    assert payload == {"recoverable": True, "restart_after_ms": 500}


def test_param_int_falls_back_to_default_for_non_int() -> None:
    assert param_int({"count": "not-an-int"}, "count", 7) == 7
    assert param_int({"count": True}, "count", 7) == 7  # bool excluded even though int subclass
    assert param_int({"count": 3}, "count", 7) == 3
    assert param_int({}, "count", 7) == 7
