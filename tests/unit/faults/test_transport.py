"""Unit tests for `runtime.faults.transport` — `latency` and `message_drop` (PRD §12.2)."""

from __future__ import annotations

from agentdx.events.schema import EventType
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.faults.transport import TransportFaultInjector
from agentdx.scenario.schema import TargetKind
from tests.unit.faults.conftest import ValidatingStamp, resolved_scenario


def _injector(faults: list[dict[str, object]], *, seed: int = 1) -> TransportFaultInjector:
    resolved = resolved_scenario(faults=faults)
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    stamp = ValidatingStamp()
    taint = FaultTaintTracker()
    return TransportFaultInjector(registry=registry, seed=seed, stamp=stamp, taint=taint)


def test_constant_pattern_applies_the_same_delay_every_fire() -> None:
    injector = _injector(
        [{"type": "latency", "edge": "planner->coder", "always": True, "delay_ms": 200}]
    )
    for _ in range(3):
        decision = injector.decide_latency(
            target_kind=TargetKind.EDGE, target="planner->coder", virtual_ts_ms=0
        )
        assert decision.extra_delay_ms == 200
        assert decision.armed is not None


def test_spike_pattern_applies_delay_only_on_first_fire() -> None:
    injector = _injector(
        [
            {
                "type": "latency",
                "edge": "planner->coder",
                "always": True,
                "delay_ms": 200,
                "pattern": "spike",
            }
        ]
    )
    first = injector.decide_latency(
        target_kind=TargetKind.EDGE, target="planner->coder", virtual_ts_ms=0
    )
    second = injector.decide_latency(
        target_kind=TargetKind.EDGE, target="planner->coder", virtual_ts_ms=1
    )
    third = injector.decide_latency(
        target_kind=TargetKind.EDGE, target="planner->coder", virtual_ts_ms=2
    )
    assert first.extra_delay_ms == 200
    assert second.extra_delay_ms == 0
    assert third.extra_delay_ms == 0


def test_degrade_pattern_worsens_linearly_with_fire_count() -> None:
    injector = _injector(
        [
            {
                "type": "latency",
                "edge": "planner->coder",
                "always": True,
                "delay_ms": 100,
                "pattern": "degrade",
            }
        ]
    )
    delays = [
        injector.decide_latency(
            target_kind=TargetKind.EDGE, target="planner->coder", virtual_ts_ms=i
        ).extra_delay_ms
        for i in range(3)
    ]
    assert delays == [100, 200, 300]


def test_latency_targeting_agent_kind_does_not_match_a_differently_kinded_target() -> None:
    injector = _injector([{"type": "latency", "agent": "reviewer", "always": True, "delay_ms": 50}])
    # Same target string, wrong kind (EDGE vs AGENT) -> no match.
    decision = injector.decide_latency(
        target_kind=TargetKind.EDGE, target="reviewer", virtual_ts_ms=0
    )
    assert decision.armed is None
    assert decision.extra_delay_ms == 0

    decision2 = injector.decide_latency(
        target_kind=TargetKind.AGENT, target="reviewer", virtual_ts_ms=0
    )
    assert decision2.armed is not None


def test_latency_emits_fault_injected_once_and_fault_effect_per_fire() -> None:
    injector = _injector([{"type": "latency", "edge": "a->b", "always": True, "delay_ms": 10}])
    injector.decide_latency(target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=0)
    injector.decide_latency(target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=1)

    stamp: ValidatingStamp = injector._stamp  # type: ignore[assignment]
    injected = [e for e in stamp.events if e.type is EventType.FAULT_INJECTED]
    effects = [e for e in stamp.events if e.type is EventType.FAULT_EFFECT]
    assert len(injected) == 1
    assert len(effects) == 2
    assert all(e.payload["effect"] == "delay" for e in effects)


def test_message_drop_at_probability_1000_always_drops_when_triggered() -> None:
    injector = _injector(
        [
            {
                "type": "message_drop",
                "edge": "a->b",
                "always": True,
                "probability_permille": 1000,
            }
        ]
    )
    decision = injector.decide_drop(edge="a->b", virtual_ts_ms=0)
    assert decision.dropped is True
    assert decision.armed is not None


def test_message_drop_at_probability_0_never_drops() -> None:
    injector = _injector(
        [{"type": "message_drop", "edge": "a->b", "always": True, "probability_permille": 0}]
    )
    decision = injector.decide_drop(edge="a->b", virtual_ts_ms=0)
    assert decision.dropped is False
    assert decision.armed is None


def test_message_drop_fault_effect_schema_is_valid() -> None:
    injector = _injector(
        [{"type": "message_drop", "edge": "a->b", "always": True, "probability_permille": 1000}]
    )
    injector.decide_drop(edge="a->b", virtual_ts_ms=0)
    stamp: ValidatingStamp = injector._stamp  # type: ignore[assignment]
    effects = [e for e in stamp.events if e.type is EventType.FAULT_EFFECT]
    assert len(effects) == 1
    assert effects[0].payload["effect"] == "drop"
    assert effects[0].payload["target"] == "a->b"


def test_untriggered_fault_produces_no_events_and_no_decision() -> None:
    injector = _injector(
        [{"type": "latency", "edge": "a->b", "at_virtual_ts": 5000, "delay_ms": 10}]
    )
    decision = injector.decide_latency(
        target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=100
    )
    assert decision.armed is None
    stamp: ValidatingStamp = injector._stamp  # type: ignore[assignment]
    assert stamp.events == []
