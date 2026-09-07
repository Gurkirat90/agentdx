"""Unit tests for `runtime.faults.dependency` — `tool_failure` (PRD §12.2)."""

from __future__ import annotations

import pytest

from agentdx.events.schema import EventType
from agentdx.runtime.faults.dependency import DependencyFaultInjector
from agentdx.runtime.faults.registry import BlastRadius, FaultRegistry
from agentdx.runtime.faults.safety import ChaosAuthorizationError
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


# ---------------------------------------------------------------------------------------
# Determinism (op2-audit-p09-second.md finding #4) — a genuine PROBABILITY-triggered fault
# ---------------------------------------------------------------------------------------


def test_probability_triggered_tool_failure_matches_the_pinned_permille_sequence() -> None:
    """A `PROBABILITY`-triggered `tool_failure`'s fire sequence, against pinned reference values.

    op2-audit-p09-second.md finding #4: the only existing "determinism with faults" gate armed
    exactly one `agent_crash`, a fault class with no `PROBABILITY` path, so it structurally
    could not exercise `FaultRandomStream` at all — rigging the stream to raise on its first
    draw left that gate green. `tool_failure` genuinely supports `TriggerKind.PROBABILITY`
    (`scenario.schema.FAULT_CATALOGUE["tool_failure"].trigger_kinds`); this test arms one at
    seed 42 and compares its real fire/no-fire sequence against permille values computed
    independently — via a fresh `hashlib.blake2b` call matching `FaultRandomStream.
    next_permille`'s own documented algorithm, not by calling `next_permille` itself — so a
    `% 1000` -> `% 100` modulus inversion (or any other change to the draw algorithm) fails
    this test, unlike `test_triggers.py::test_probability_trigger_matches_stream_draw_exactly`,
    whose two sides both call the same production function and so cannot catch that class of
    bug (see this repo's op2-audit-p09-second.md finding #4 for the live demonstration).
    """
    # Independently computed: `int.from_bytes(blake2b(f"42:{i}".encode(), digest_size=8)
    # .digest(), "big") % 1000` for i = 1..10, run in a throwaway script, not through
    # `FaultRandomStream` — pinned here as literals per the audit's own suggested fix.
    expected_permille = [103, 624, 129, 312, 364, 881, 744, 738, 210, 23]
    threshold = 500
    expected_fires = [v < threshold for v in expected_permille]

    injector = _injector(
        [
            {
                "type": "tool_failure",
                "tool": "deploy",
                "probability_permille": threshold,
                "count": 1,
            }
        ],
        seed=42,
    )
    actual_fires = [
        injector.decide_tool_call(tool="deploy", virtual_ts_ms=i).should_fail
        for i in range(len(expected_permille))
    ]
    assert actual_fires == expected_fires


def test_probability_triggered_tool_failure_is_reproducible_across_fresh_injectors() -> None:
    """Same seed, two independently-constructed injectors -> identical fire sequences (I1)."""
    faults = [{"type": "tool_failure", "tool": "deploy", "probability_permille": 500, "count": 1}]
    injector_a = _injector(faults, seed=42)
    injector_b = _injector(faults, seed=42)
    fires_a = [
        injector_a.decide_tool_call(tool="deploy", virtual_ts_ms=i).should_fail for i in range(15)
    ]
    fires_b = [
        injector_b.decide_tool_call(tool="deploy", virtual_ts_ms=i).should_fail for i in range(15)
    ]
    assert fires_a == fires_b
    assert any(fires_a)  # the fixture is decisive, not vacuously "never fires"
    assert not all(fires_a)  # ... and not vacuously "always fires" either


# ---------------------------------------------------------------------------------------
# Fire-time reauthorization (op2-audit-p09-second.md finding #3)
# ---------------------------------------------------------------------------------------


def test_fire_time_reauthorization_refuses_tool_failure_after_radius_narrows() -> None:
    """PRD §13.4's runtime defence-in-depth check, through a real `DependencyFaultInjector`.

    Same shape as the equivalent `process.py`/`transport.py` tests: arm inside the radius,
    narrow the live registry's `blast_radius` after arming, then fire — deleting
    `dependency.py`'s `safety.reauthorize` call must turn this test red.
    """
    resolved = resolved_scenario(
        faults=[{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1}],
        chaos_opt_in=True,
        blast_radius={"tools": ["deploy"]},
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    stamp = ValidatingStamp()
    taint = FaultTaintTracker()
    injector = DependencyFaultInjector(registry=registry, seed=1, stamp=stamp, taint=taint)

    registry.blast_radius = BlastRadius(tools=frozenset({"other_tool"}))

    with pytest.raises(ChaosAuthorizationError) as excinfo:
        injector.decide_tool_call(tool="deploy", virtual_ts_ms=0)
    assert "E-CHAOS-001" in str(excinfo.value)
