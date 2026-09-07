"""Unit tests for `runtime.faults.transport` — `latency` and `message_drop` (PRD §12.2)."""

from __future__ import annotations

import pytest

from agentdx.events.schema import EventType
from agentdx.runtime.faults.registry import BlastRadius, FaultRegistry
from agentdx.runtime.faults.safety import ChaosAuthorizationError
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.faults.transport import TransportFaultInjector
from agentdx.scenario.schema import TargetKind
from tests.unit.faults.conftest import ValidatingStamp, resolved_scenario


def _injector(
    faults: list[dict[str, object]],
    *,
    seed: int = 1,
    max_virtual_duration_ms: int | None = None,
) -> TransportFaultInjector:
    resolved = resolved_scenario(faults=faults)
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    stamp = ValidatingStamp()
    taint = FaultTaintTracker()
    return TransportFaultInjector(
        registry=registry,
        seed=seed,
        stamp=stamp,
        taint=taint,
        max_virtual_duration_ms=max_virtual_duration_ms,
    )


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


def test_latency_delay_is_clamped_to_the_remaining_max_virtual_duration_budget() -> None:
    """PRD §12.2 `latency` Safety row: "Bounded by `max_virtual_duration_ms`".

    op2-audit-p09-second.md finding #2: `TransportFaultInjector` had no
    `max_virtual_duration_ms` parameter at all before this fix — a `degrade`-pattern fault
    with a large `delay_ms` grew its proposed delay without any ceiling, six calls in a row,
    demonstrated live against the unpatched class. With the budget threaded in, a delay that
    would push the run past `virtual_ts_ms + delay_ms > max_virtual_duration_ms` is clamped to
    whatever of the budget remains, never left free to exceed it.
    """
    injector = _injector(
        [{"type": "latency", "edge": "a->b", "always": True, "delay_ms": 5000}],
        max_virtual_duration_ms=12_000,
    )
    delays = [
        injector.decide_latency(
            target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=i * 1000
        ).extra_delay_ms
        for i in range(6)
    ]
    # constant pattern: raw proposed delay is always 5000, clamped against (12000 - now).
    assert delays == [5000, 5000, 5000, 5000, 5000, 5000]
    # Confirm the clamp actually engages once the raw proposal would exceed the budget: a
    # fresh injector whose virtual_ts_ms starts close to the ceiling.
    injector2 = _injector(
        [{"type": "latency", "edge": "a->b", "always": True, "delay_ms": 5000}],
        max_virtual_duration_ms=12_000,
    )
    decision = injector2.decide_latency(
        target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=10_000
    )
    assert decision.extra_delay_ms == 2000  # min(5000, max(0, 12000 - 10000))


def test_latency_delay_clamps_to_zero_once_the_budget_is_already_exhausted() -> None:
    injector = _injector(
        [{"type": "latency", "edge": "a->b", "always": True, "delay_ms": 5000}],
        max_virtual_duration_ms=1000,
    )
    decision = injector.decide_latency(
        target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=5000
    )
    assert decision.extra_delay_ms == 0  # max(0, 1000 - 5000) == 0, no negative delay
    assert decision.armed is not None  # the fault still fired — its effect is just capped


def test_latency_with_no_max_virtual_duration_ms_is_unbounded_as_before() -> None:
    """No budget given (the default) — behavior unchanged from before this fix."""
    injector = _injector([{"type": "latency", "edge": "a->b", "always": True, "delay_ms": 5000}])
    decision = injector.decide_latency(target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=0)
    assert decision.extra_delay_ms == 5000


def test_decide_drop_never_drops_a_delivery_carrying_run_end() -> None:
    """PRD §12.2 `message_drop` Safety row: "Cannot drop a `run_end` control message".

    op2-audit-p09-second.md finding #2: `decide_drop` had no parameter through which a caller
    could express "this delivery carries run_end" at all before this fix — the rule could not
    be enforced even by a conscientious caller. A probability-1000 (always-drop-when-triggered)
    fault would otherwise certainly drop this delivery; `carries_run_end=True` refuses it
    unconditionally instead, and produces no event and no stream draw.
    """
    injector = _injector(
        [{"type": "message_drop", "edge": "a->b", "always": True, "probability_permille": 1000}]
    )
    decision = injector.decide_drop(edge="a->b", virtual_ts_ms=0, carries_run_end=True)
    assert decision.dropped is False
    assert decision.armed is None
    stamp: ValidatingStamp = injector._stamp  # type: ignore[assignment]
    assert stamp.events == []  # no fault_injected/fault_effect event for a refused drop


def test_decide_drop_still_drops_a_non_run_end_delivery_on_the_same_edge() -> None:
    """The `carries_run_end` refusal is per-call, not a blanket suppression of the whole fault."""
    injector = _injector(
        [{"type": "message_drop", "edge": "a->b", "always": True, "probability_permille": 1000}]
    )
    refused = injector.decide_drop(edge="a->b", virtual_ts_ms=0, carries_run_end=True)
    normal = injector.decide_drop(edge="a->b", virtual_ts_ms=1, carries_run_end=False)
    assert refused.dropped is False
    assert normal.dropped is True


def test_fire_time_reauthorization_refuses_latency_whose_radius_narrowed_after_arming() -> None:
    """PRD §13.4's runtime defence-in-depth check, through a real `TransportFaultInjector`.

    op2-audit-p09-second.md finding #3: every real fire-time reauthorization test in this
    suite called `safety.reauthorize` directly, never through a real injector encountering a
    blast radius narrowed after arming. This test arms `latency` inside the radius, narrows
    the live registry's `blast_radius` after arming, then calls `decide_latency` — deleting
    `transport.py`'s `safety.reauthorize` call must turn this test red.
    """
    resolved = resolved_scenario(
        faults=[{"type": "latency", "edge": "a->b", "always": True, "delay_ms": 100}],
        chaos_opt_in=True,
        blast_radius={"edges": ["a->b"]},
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    stamp = ValidatingStamp()
    taint = FaultTaintTracker()
    injector = TransportFaultInjector(registry=registry, seed=1, stamp=stamp, taint=taint)

    registry.blast_radius = BlastRadius(edges=frozenset({"x->y"}))  # "a->b" no longer authorised

    with pytest.raises(ChaosAuthorizationError) as excinfo:
        injector.decide_latency(target_kind=TargetKind.EDGE, target="a->b", virtual_ts_ms=0)
    assert "E-CHAOS-001" in str(excinfo.value)


def test_fire_time_reauthorization_refuses_message_drop_whose_radius_narrowed_after_arming() -> (
    None
):
    """Same as above, for `message_drop` — a separate call site (`decide_drop`)."""
    resolved = resolved_scenario(
        faults=[
            {
                "type": "message_drop",
                "edge": "a->b",
                "always": True,
                "probability_permille": 1000,
            }
        ],
        chaos_opt_in=True,
        blast_radius={"edges": ["a->b"]},
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    stamp = ValidatingStamp()
    taint = FaultTaintTracker()
    injector = TransportFaultInjector(registry=registry, seed=1, stamp=stamp, taint=taint)

    registry.blast_radius = BlastRadius(edges=frozenset({"x->y"}))

    with pytest.raises(ChaosAuthorizationError) as excinfo:
        injector.decide_drop(edge="a->b", virtual_ts_ms=0)
    assert "E-CHAOS-001" in str(excinfo.value)


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
