"""Unit tests for `runtime.faults.triggers` — PRD §12.3's `should_fire`, and determinism."""

from __future__ import annotations

from agentdx.runtime.faults.registry import ArmedFault, FaultDecl, Trigger
from agentdx.runtime.faults.triggers import (
    REPEATING_TRIGGER_KINDS,
    FaultRandomStream,
    seeded_stream,
    should_fire,
)
from agentdx.scenario.schema import TargetKind, TriggerKind


def _armed(kind: TriggerKind, value: int | str | None, *, fault_id: str = "f_00") -> ArmedFault:
    decl = FaultDecl(
        fault_id=fault_id,
        fault_type="agent_crash",
        target_kind=TargetKind.AGENT,
        target="reviewer",
        trigger=Trigger(kind=kind, value=value),
        params={},
    )
    return ArmedFault(decl=decl)


def test_at_virtual_ts_fires_at_and_after_the_timestamp_not_before() -> None:
    armed = _armed(TriggerKind.AT_VIRTUAL_TS, 3000)
    stream = seeded_stream(1)
    assert should_fire(armed, virtual_ts_ms=2999, stream=stream) is False
    assert should_fire(armed, virtual_ts_ms=3000, stream=stream) is True
    assert should_fire(armed, virtual_ts_ms=3001, stream=stream) is True


def test_at_virtual_ts_does_not_refire_once_marked_fired() -> None:
    armed = _armed(TriggerKind.AT_VIRTUAL_TS, 3000)
    armed.record_fire(virtual_ts_ms=3000, target="reviewer")
    stream = seeded_stream(1)
    assert should_fire(armed, virtual_ts_ms=5000, stream=stream) is False


def test_at_span_n_fires_only_at_exact_count() -> None:
    armed = _armed(TriggerKind.AT_SPAN_N, 2)
    stream = seeded_stream(1)
    assert should_fire(armed, virtual_ts_ms=0, stream=stream, span_count=1) is False
    assert should_fire(armed, virtual_ts_ms=0, stream=stream, span_count=2) is True
    assert should_fire(armed, virtual_ts_ms=0, stream=stream, span_count=3) is False


def test_at_span_n_with_no_span_count_supplied_never_fires() -> None:
    armed = _armed(TriggerKind.AT_SPAN_N, 2)
    stream = seeded_stream(1)
    assert should_fire(armed, virtual_ts_ms=0, stream=stream) is False


def test_after_n_messages_fires_at_and_beyond_threshold() -> None:
    armed = _armed(TriggerKind.AFTER_N_MESSAGES, 4)
    stream = seeded_stream(1)
    assert should_fire(armed, virtual_ts_ms=0, stream=stream, message_count=3) is False
    assert should_fire(armed, virtual_ts_ms=0, stream=stream, message_count=4) is True
    assert should_fire(armed, virtual_ts_ms=0, stream=stream, message_count=9) is True


def test_on_state_write_fires_only_for_the_declared_key() -> None:
    armed = _armed(TriggerKind.ON_STATE_WRITE, "draft.body")
    stream = seeded_stream(1)
    assert (
        should_fire(armed, virtual_ts_ms=0, stream=stream, current_state_write_key="other") is False
    )
    assert (
        should_fire(armed, virtual_ts_ms=0, stream=stream, current_state_write_key="draft.body")
        is True
    )


def test_always_fires_every_time_including_after_already_fired() -> None:
    armed = _armed(TriggerKind.ALWAYS, None)
    stream = seeded_stream(1)
    assert should_fire(armed, virtual_ts_ms=0, stream=stream) is True
    armed.record_fire(virtual_ts_ms=0, target="reviewer")
    assert should_fire(armed, virtual_ts_ms=100, stream=stream) is True


def test_probability_trigger_matches_stream_draw_exactly() -> None:
    # Hand-computed: draw the stream independently at the same seed and compare should_fire's
    # own decision against a bare `next_permille() < value` check on an identically-seeded,
    # freshly-constructed stream (should_fire must consume exactly one permille per call).
    armed = _armed(TriggerKind.PROBABILITY, 500)
    live_stream = seeded_stream(7)
    reference_stream = seeded_stream(7)
    for _ in range(10):
        expected = reference_stream.next_permille() < 500
        actual = should_fire(armed, virtual_ts_ms=0, stream=live_stream)
        assert actual == expected


def test_probability_trigger_refires_after_already_fired() -> None:
    armed = _armed(TriggerKind.PROBABILITY, 1000)  # always draws True (permille < 1000 always)
    armed.record_fire(virtual_ts_ms=0, target="reviewer")
    stream = seeded_stream(3)
    assert should_fire(armed, virtual_ts_ms=0, stream=stream) is True


def test_repeating_trigger_kinds_is_exactly_probability_and_always() -> None:
    assert REPEATING_TRIGGER_KINDS == frozenset({TriggerKind.PROBABILITY, TriggerKind.ALWAYS})


def test_fault_random_stream_is_deterministic_for_the_same_seed() -> None:
    a = FaultRandomStream(seed=123)
    b = FaultRandomStream(seed=123)
    draws_a = [a.next_permille() for _ in range(50)]
    draws_b = [b.next_permille() for _ in range(50)]
    assert draws_a == draws_b


def test_fault_random_stream_differs_across_seeds() -> None:
    a = FaultRandomStream(seed=1)
    b = FaultRandomStream(seed=2)
    draws_a = [a.next_permille() for _ in range(20)]
    draws_b = [b.next_permille() for _ in range(20)]
    assert draws_a != draws_b


def test_fault_random_stream_values_are_in_range() -> None:
    stream = FaultRandomStream(seed=99)
    for _ in range(200):
        value = stream.next_permille()
        assert 0 <= value < 1000
