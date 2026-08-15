"""Unit tests for `runtime.clock`: `VirtualClock`, `wall_time`, and calibration profiles."""

from __future__ import annotations

import time

import pytest

from agentdx.config import SchedulerConfig
from agentdx.runtime.clock import (
    CalibrationProfile,
    CalibrationSample,
    ClockError,
    VirtualClock,
    drift_permille,
    wall_time,
)

# ---------------------------------------------------------------------------------------
# VirtualClock (PRD §10.3)
# ---------------------------------------------------------------------------------------


def test_virtual_clock_starts_at_zero_by_default() -> None:
    assert VirtualClock().now_ms() == 0


def test_virtual_clock_starts_at_a_given_offset() -> None:
    assert VirtualClock(start_ms=500).now_ms() == 500


def test_virtual_clock_cannot_start_negative() -> None:
    with pytest.raises(ClockError):
        VirtualClock(start_ms=-1)


def test_advance_to_moves_the_clock_forward() -> None:
    clock = VirtualClock()
    clock.advance_to(100)
    assert clock.now_ms() == 100


def test_advance_to_the_same_instant_is_a_no_op_not_an_error() -> None:
    clock = VirtualClock(start_ms=50)
    clock.advance_to(50)
    assert clock.now_ms() == 50


def test_advance_to_backward_raises() -> None:
    clock = VirtualClock(start_ms=100)
    with pytest.raises(ClockError):
        clock.advance_to(99)


def test_advance_by_accumulates() -> None:
    clock = VirtualClock()
    clock.advance_by(30)
    clock.advance_by(20)
    assert clock.now_ms() == 50


def test_advance_by_negative_raises() -> None:
    clock = VirtualClock()
    with pytest.raises(ClockError):
        clock.advance_by(-1)


def test_virtual_clock_never_reads_the_real_clock() -> None:
    """Two clocks, same operations, must agree regardless of wall time elapsed between them."""
    a = VirtualClock()
    a.advance_by(123)
    time.sleep(0.01)  # real time passes; must not leak into virtual time
    b = VirtualClock()
    b.advance_by(123)
    assert a.now_ms() == b.now_ms() == 123


def test_virtual_ms_and_wall_ms_satisfy_the_sdk_generic_clock_protocol() -> None:
    clock = VirtualClock(start_ms=42)
    assert clock.virtual_ms() == 42
    assert clock.wall_ms() == 0  # a bare VirtualClock has no wall-time notion


# ---------------------------------------------------------------------------------------
# wall_time() — the sanctioned real-clock accessor
# ---------------------------------------------------------------------------------------


def test_wall_time_returns_a_positive_integer_never_a_float() -> None:
    value = wall_time()
    assert isinstance(value, int)
    assert not isinstance(value, bool)
    assert value > 0


def test_wall_time_is_non_decreasing_across_two_calls() -> None:
    first = wall_time()
    second = wall_time()
    assert second >= first


# ---------------------------------------------------------------------------------------
# Calibration (PRD §10.4)
# ---------------------------------------------------------------------------------------


def test_defaults_only_profile_uses_the_q43_2_3_documented_defaults() -> None:
    config = SchedulerConfig()
    profile = CalibrationProfile.defaults_only(config)
    assert profile.duration_for(agent_id="coder", kind="llm_call", name="chat") == 800
    assert profile.duration_for(agent_id="coder", kind="tool_call", name="search") == 200
    assert profile.duration_for(agent_id="coder", kind="agent_step", name="step") == 50


def test_uncalibrated_kinds_default_to_zero_not_a_fabricated_number() -> None:
    profile = CalibrationProfile.defaults_only(SchedulerConfig())
    assert profile.duration_for(agent_id="coder", kind="handoff", name="h") == 0
    assert profile.duration_for(agent_id="coder", kind="wait", name="w") == 0


def test_group_entry_beats_kind_entry_beats_default() -> None:
    config = SchedulerConfig()
    samples = [
        CalibrationSample(agent_id="coder", kind="llm_call", name="chat", duration_wall_ms=900),
        CalibrationSample(agent_id="reviewer", kind="llm_call", name="chat", duration_wall_ms=700),
    ]
    profile = CalibrationProfile.from_samples(samples, config=config, calibration_id="cal-1")

    # Exact group match wins.
    assert profile.duration_for(agent_id="coder", kind="llm_call", name="chat") == 900
    # No group match for this name -> falls back to the kind-level median across both samples.
    # Nearest-rank median of the sorted pair [700, 900] at the 50th percentile is rank
    # ceil(50*2/100) = 1 -> the lower value, 700 (not an average of the two — no float
    # is ever constructed, per `_nearest_rank`'s own docstring).
    assert profile.duration_for(agent_id="coder", kind="llm_call", name="other") == 700
    # No samples for tool_call at all -> the Q-43.2.3 default.
    assert profile.duration_for(agent_id="coder", kind="tool_call", name="x") == 200


def test_median_and_p90_use_integer_nearest_rank_never_a_float() -> None:
    config = SchedulerConfig()
    samples = [
        CalibrationSample(agent_id="a", kind="tool_call", name="t", duration_wall_ms=ms)
        for ms in (100, 200, 300, 400, 500)
    ]
    profile = CalibrationProfile.from_samples(samples, config=config, calibration_id="cal-2")
    entry = profile.by_group[("a", "tool_call", "t")]
    assert isinstance(entry.median_ms, int)
    assert isinstance(entry.p90_ms, int)
    assert entry.p90_ms >= entry.median_ms
    assert entry.samples == 5


def test_single_sample_group_has_equal_median_and_p90() -> None:
    config = SchedulerConfig()
    samples = [CalibrationSample(agent_id="a", kind="tool_call", name="t", duration_wall_ms=42)]
    profile = CalibrationProfile.from_samples(samples, config=config, calibration_id="cal-3")
    entry = profile.by_group[("a", "tool_call", "t")]
    assert entry.median_ms == entry.p90_ms == 42


def test_jitter_off_by_default_is_deterministic_without_an_rng() -> None:
    config = SchedulerConfig()
    samples = [
        CalibrationSample(agent_id="a", kind="tool_call", name="t", duration_wall_ms=ms)
        for ms in (100, 300)
    ]
    profile = CalibrationProfile.from_samples(samples, config=config, calibration_id="cal-4")
    # No rng passed, jitter off: must return exactly the median every time.
    for _ in range(5):
        assert profile.duration_for(agent_id="a", kind="tool_call", name="t") == 100


def test_jitter_on_draws_from_the_injected_seeded_source_only() -> None:
    import random

    config = SchedulerConfig()
    samples = [
        CalibrationSample(agent_id="a", kind="tool_call", name="t", duration_wall_ms=ms)
        for ms in (100, 300)
    ]
    profile = CalibrationProfile.from_samples(
        samples, config=config, calibration_id="cal-5", jitter=True
    )
    rng = random.Random(42)  # noqa: S311 — a seeded RandomSource under test, not crypto use
    values = {
        profile.duration_for(agent_id="a", kind="tool_call", name="t", rng=rng) for _ in range(20)
    }
    entry = profile.by_group[("a", "tool_call", "t")]
    assert all(entry.median_ms <= v <= entry.p90_ms for v in values)
    # Same seed, same draw sequence -> identical results on replay.
    rng_a = random.Random(99)  # noqa: S311 — seeded RandomSource, not crypto use
    rng_b = random.Random(99)  # noqa: S311 — seeded RandomSource, not crypto use
    seq_a = [
        profile.duration_for(agent_id="a", kind="tool_call", name="t", rng=rng_a) for _ in range(10)
    ]
    seq_b = [
        profile.duration_for(agent_id="a", kind="tool_call", name="t", rng=rng_b) for _ in range(10)
    ]
    assert seq_a == seq_b


def test_drift_permille_is_zero_when_expected_is_zero() -> None:
    assert drift_permille(100, 0) == 0


def test_drift_permille_computes_an_integer_ratio() -> None:
    # 110 vs 100 expected is a 10% divergence -> 100 per-mille.
    assert drift_permille(110, 100) == 100
    assert drift_permille(100, 100) == 0
