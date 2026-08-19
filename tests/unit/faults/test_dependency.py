"""Unit tests for `runtime.faults.dependency` — `tool_failure` (PRD §12.2)."""

from __future__ import annotations

from agentdx.events.schema import EventType
from agentdx.runtime.faults.dependency import DependencyFaultInjector
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.taint import FaultTaintTracker
from tests.unit.faults.conftest import ValidatingStamp, resolved_scenario


def _injector(faults: list[dict[str, object]], *, seed: int = 1) -> DependencyFaultInjector:
    resolved = resolved_scenario(faults=faults)
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    stamp = ValidatingStamp()
    taint = FaultTaintTracker()
    return DependencyFaultInjector(registry=registry, seed=seed, stamp=stamp, taint=taint)


def test_at_virtual_ts_with_count_1_fails_exactly_one_call() -> None:
    injector = _injector(
        [{"type": "tool_failure", "tool": "deploy", "at_virtual_ts": 1000, "count": 1}]
    )
    first = injector.decide_tool_call(tool="deploy", virtual_ts_ms=1000)
    second = injector.decide_tool_call(tool="deploy", virtual_ts_ms=1001)
    assert first.should_fail is True
    assert second.should_fail is False


def test_at_virtual_ts_with_count_3_fails_three_consecutive_calls_then_stops() -> None:
    injector = _injector(
        [{"type": "tool_failure", "tool": "deploy", "at_virtual_ts": 1000, "count": 3}]
    )
    results = [
        injector.decide_tool_call(tool="deploy", virtual_ts_ms=1000 + i).should_fail
        for i in range(5)
    ]
    assert results == [True, True, True, False, False]


def test_before_the_trigger_timestamp_never_fails() -> None:
    injector = _injector(
        [{"type": "tool_failure", "tool": "deploy", "at_virtual_ts": 1000, "count": 1}]
    )
    result = injector.decide_tool_call(tool="deploy", virtual_ts_ms=999)
    assert result.should_fail is False
    assert result.armed is None


def test_always_trigger_fails_every_call() -> None:
    injector = _injector([{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1}])
    for i in range(5):
        assert injector.decide_tool_call(tool="deploy", virtual_ts_ms=i).should_fail is True


def test_mode_is_reported_verbatim_on_the_decision() -> None:
    injector = _injector(
        [
            {
                "type": "tool_failure",
                "tool": "deploy",
                "always": True,
                "count": 1,
                "mode": "429",
            }
        ]
    )
    result = injector.decide_tool_call(tool="deploy", virtual_ts_ms=0)
    assert result.mode == "429"


def test_fault_effect_payload_maps_mode_to_exception_type() -> None:
    injector = _injector(
        [{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1, "mode": "500"}]
    )
    injector.decide_tool_call(tool="deploy", virtual_ts_ms=0)
    stamp: ValidatingStamp = injector._stamp  # type: ignore[assignment]
    effects = [e for e in stamp.events if e.type is EventType.FAULT_EFFECT]
    assert len(effects) == 1
    assert effects[0].payload["effect"] == "exception"
    assert effects[0].payload["exception_type"] == "ToolServerError"
    assert effects[0].payload["target"] == "deploy"


def test_a_different_tool_is_never_affected() -> None:
    injector = _injector([{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1}])
    result = injector.decide_tool_call(tool="other_tool", virtual_ts_ms=0)
    assert result.should_fail is False


def test_fault_injected_emitted_exactly_once_across_multiple_fires() -> None:
    injector = _injector([{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1}])
    for i in range(4):
        injector.decide_tool_call(tool="deploy", virtual_ts_ms=i)
    stamp: ValidatingStamp = injector._stamp  # type: ignore[assignment]
    injected = [e for e in stamp.events if e.type is EventType.FAULT_INJECTED]
    effects = [e for e in stamp.events if e.type is EventType.FAULT_EFFECT]
    assert len(injected) == 1
    assert len(effects) == 4
