"""The virtual clock and wall-clock calibration profiles (PRD §10.3, §10.4, invariant I11).

**Why this file is allowlisted (AGENTS.md §4.1 clause 2 — "owns virtual time").** Virtual
time has to come from *somewhere*: `VirtualClock` starts at 0 and only ever advances by an
amount the scheduler decides (PRD §10.3's `assert ts >= self.now_ms`), so nothing in this
file needs the real clock to compute virtual time. The one exception is `wall_time()` itself
— the sanctioned accessor AGENTS.md §4.1 clause 3 names and `agentdx/__init__.py` has been
waiting on since P04 (CONTEXT.md D-16, D-20's docstring). Every other module in the tree
reaches the real clock only by calling this function; there is no second door.

**No floats anywhere in this file.** PRD §10.3: "No floats anywhere in the clock — float
accumulation is a determinism leak across architectures." Every duration, timestamp and
percentile computed here is integer milliseconds, computed with integer arithmetic
(`-(-a // b)` for ceiling division rather than `math.ceil(a / b)`), never rounded from a
float.

PRD §10.3 (virtual clock), §10.4 (calibration), §10.10 (`calibration_id` provenance),
Q-43.2.3 (calibration defaults, ACCEPTED as the recommended default — CONTEXT.md §10).
"""

from __future__ import annotations

import time  # determinism-exempt: §4.1(2) owns virtual time — the one real-clock read in the file
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

from agentdx.config import SchedulerConfig

_DOCS: Final = "docs/determinism-guarantees.md"

SPAN_KINDS: Final = ("llm_call", "tool_call", "agent_step", "handoff", "wait")
"""Mirrors `events.schema.PAYLOAD_SCHEMAS[EventType.SPAN_START]["kind"].enum`. Not imported
from there: `events/schema.py` has no notion of a calibration profile, and a runtime-side
tuple of the same five strings is cheaper than adding a second axis to the event contract."""

CALIBRATED_KINDS: Final = ("llm_call", "tool_call", "agent_step")
"""Q-43.2.3 gives a documented default for exactly these three kinds. `handoff` and `wait`
are scheduling artefacts with no real-world wall-clock analogue to calibrate against; a span
of either kind that reaches `CalibrationProfile.duration_for` with no group entry gets 0ms,
which is honest — there is no PRD-specified default to fall back to."""


class ClockError(RuntimeError):
    """The virtual clock was asked to do something that would break I11.

    Carries `E-SCHED-001` — the one member of the `E-SCHED-00x` family PRD §36 leaves
    unassigned. Reserved here for a scheduler/clock-internal invariant violation, as opposed
    to a condition a user's graph can trigger (those are `E-SCHED-002`/`003`/`004`, all
    specified). Declared per `docs/determinism-guarantees.md` §5.
    """

    code: Final = "E-SCHED-001"

    def __init__(self, detail: str) -> None:
        """Build the error from a description of what invariant would have broken."""
        super().__init__(f"[{self.code}] {detail} ({_DOCS}#{self.code.lower()})")


# ---------------------------------------------------------------------------------------
# The virtual clock (PRD §10.3)
# ---------------------------------------------------------------------------------------


class VirtualClock:
    """Monotonic, integer-millisecond virtual time (PRD §10.3, invariant I11).

    Guarantees: `now_ms()` never decreases across the clock's lifetime; `advance_to` accepts
    only a timestamp at or after the current one, exactly the PRD pseudocode's
    `assert ts >= self.now_ms`, turned into a typed, documented error instead of a bare
    assert (AGENTS.md §4: every public function that can fail states its failure mode). Time
    advances only when the scheduler calls one of these two methods — never on its own, never
    from a background thread, never from anything reading the real clock.
    """

    __slots__ = ("_now_ms",)

    def __init__(self, start_ms: int = 0) -> None:
        """Start the clock at `start_ms` (0 for a fresh run).

        Raises:
            ClockError: `start_ms` is negative.
        """
        if start_ms < 0:
            detail = f"a virtual clock cannot start negative, got {start_ms}"
            raise ClockError(detail)
        self._now_ms = start_ms

    def now_ms(self) -> int:
        """Return the current virtual time in milliseconds since run start."""
        return self._now_ms

    def advance_to(self, ts_ms: int) -> None:
        """Move the clock forward to exactly `ts_ms`.

        Guarantees: a no-op is not silently accepted as a "no earlier than" call — `ts_ms`
        equal to the current time is fine (a zero-duration advance), but a regression is not
        (PRD §10.3's `assert`), because a regression would mean an event downstream of this
        call could be timestamped before an event upstream of it — a virtual-time inversion
        no analyser could make sense of.

        Raises:
            ClockError: `ts_ms` is before the current time (`E-SCHED-001`).
        """
        if ts_ms < self._now_ms:
            detail = (
                f"virtual clock cannot move backward: now={self._now_ms}ms, "
                f"requested advance_to={ts_ms}ms. This is a scheduler-internal invariant "
                f"violation, not a user-triggerable condition — file a bug with the seed "
                f"and the last schedule_decision events"
            )
            raise ClockError(detail)
        self._now_ms = ts_ms

    def advance_by(self, ms: int) -> None:
        """Move the clock forward by `ms` milliseconds (a calibrated span duration).

        Raises:
            ClockError: `ms` is negative (`E-SCHED-001`) — a negative duration is not a
                virtual-time concept the clock can represent, per PRD §10.3.
        """
        if ms < 0:
            detail = f"cannot advance the virtual clock by a negative duration ({ms}ms)"
            raise ClockError(detail)
        self._now_ms += ms

    # -- sdk.generic.Clock structural conformance ---------------------------------------

    def virtual_ms(self) -> int:
        """Return `now_ms()`. Satisfies `agentdx.sdk.generic.Clock.virtual_ms` structurally."""
        return self._now_ms

    def wall_ms(self) -> int:
        """Return 0.

        `VirtualClock` alone has no notion of wall time; `sdk.generic.Clock` requires both
        methods, so a bare `VirtualClock` handed to `RunContext.create(clock=...)` is honest
        about only ever measuring one scale. `runtime/scheduler.py`'s `_SchedulerRecorder`
        is what actually pairs the two: it reads `virtual_ts_ms` from this clock's
        `now_ms()` and `wall_ts_ms` from `wall_time()` when it stamps every event — there is
        no separate `RuntimeClock` class; the pairing happens at the one stamping call site,
        not behind a second `Clock` implementation.
        """
        return 0


def wall_time() -> int:
    """Return the real wall-clock time as milliseconds since the Unix epoch.

    **This is `agentdx.wall_time()`** — the single sanctioned accessor AGENTS.md §4.1
    clause 3 names, that `agentdx/__init__.py` has documented as owed since P04
    (CONTEXT.md D-20), and that `events/writer.py`'s still-open 50ms flush timer (D-16)
    needs. Every field that may hold a real-clock value — `wall_ts_ms`,
    `payload.duration_wall_ms`, `run_start.payload.started_at_utc` — is on the PRD §10.7
    canonical-projection exclusion list, so calling this function never touches invariant I1;
    it only ever populates a field that is not compared for determinism.

    Guarantees: integer milliseconds, never a float — the return value is
    `int(time.time() * 1000)`, and the float multiplication is discarded the instant it is
    truncated, so no float ever crosses this function's boundary in either direction.
    """
    return int(
        time.time() * 1000  # determinism-exempt: §4.1(3) the sanctioned real-clock accessor
    )


# ---------------------------------------------------------------------------------------
# Wall-clock calibration (PRD §10.4)
# ---------------------------------------------------------------------------------------


@runtime_checkable
class RandomSource(Protocol):
    """The one method calibration jitter needs from a seeded RNG (PRD §10.4 "Jitter").

    Declared as a structural protocol, not imported from `random`, so this file never needs
    to name the `random` module at all — `random.Random` already satisfies this Protocol
    with no adapter, and `runtime/determinism.py` is the one place permitted to construct one
    (AGENTS.md §4.1 clause 1; `scripts/check_determinism_hygiene.py` bans any `random.*` call
    outside `runtime/clock.py` and `runtime/determinism.py`, and this file chooses not to use
    even its own allowance).
    """

    def randrange(self, stop: int) -> int:
        """Return a seeded integer in `range(stop)`."""
        ...


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """One real, wall-clock-measured span duration, as a calibration RECORDING pass observes.

    Guarantees: `duration_wall_ms` is the only field a real provider call can supply
    honestly; there is no field here that could be reconstructed from the event log alone,
    which is why calibration is its own artifact rather than something derived after the
    fact from a replay.
    """

    agent_id: str
    kind: str
    name: str
    duration_wall_ms: int


@dataclass(frozen=True, slots=True)
class CalibrationEntry:
    """The median and p90 wall-clock duration for one group, plus how many samples fed it.

    Guarantees: both durations are non-negative integers; `p90_ms >= median_ms` always,
    because p90 is defined as the 90th-percentile duration and `PRD §10.4`'s ordering can
    never invert for a non-degenerate sample (both are computed by the same nearest-rank
    method over the same sorted sample, at samples-1 apart at most).
    """

    median_ms: int
    p90_ms: int
    samples: int


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    """Durations for span kinds, keyed by group, falling back per PRD §10.4's chain.

    **Profile application (PRD §10.4, verbatim chain):** a span's virtual duration is the
    profile median for its exact `(agent_id, kind, name)` group; if absent, the global
    median for its `kind`; if that is absent too, the Q-43.2.3 documented default for that
    kind (`SchedulerConfig.calibration_*_ms` — never a literal in this file, AGENTS.md §4).

    Guarantees: `duration_for` never raises and never returns a negative number. A kind with
    no default at all (`handoff`, `wait` — see `CALIBRATED_KINDS`) and no group or kind
    entry returns 0, which is the honest answer: there is nothing to calibrate against and
    inventing a positive number would be a fabricated measurement.
    """

    calibration_id: str | None
    by_group: Mapping[tuple[str, str, str], CalibrationEntry] = field(default_factory=dict)
    by_kind: Mapping[str, CalibrationEntry] = field(default_factory=dict)
    defaults_ms: Mapping[str, int] = field(default_factory=dict)
    jitter: bool = False
    """PRD §10.4: "Off by default. If jitter=true, it is drawn from the seeded RNG so it
    remains deterministic." Never drawn from anything but the `RandomSource` passed in."""

    def duration_for(
        self, *, agent_id: str, kind: str, name: str, rng: RandomSource | None = None
    ) -> int:
        """Return the calibrated virtual duration for one span, honouring the PRD §10.4 chain.

        Guarantees: deterministic given the same `rng` state — jitter, when enabled, is
        drawn once from `rng.randrange`, which is the only place this function reaches for
        randomness, and it is always the caller's seeded source, never the `random` module.
        """
        entry = self.by_group.get((agent_id, kind, name))
        if entry is None:
            entry = self.by_kind.get(kind)
        base = entry.median_ms if entry is not None else self.defaults_ms.get(kind, 0)
        if not self.jitter or rng is None or entry is None:
            return base
        spread = entry.p90_ms - entry.median_ms
        if spread <= 0:
            return base
        return base + rng.randrange(spread + 1)

    @classmethod
    def defaults_only(
        cls, config: SchedulerConfig, *, calibration_id: str | None = None
    ) -> CalibrationProfile:
        """Return a profile with no recorded samples — every span gets the Q-43.2.3 default.

        This is what a run uses when no calibration pass has ever been recorded (the common
        case today: `agentdx run --record --calibrate`, PRD §10.4, is a CLI feature that
        lands with `cli/` at P17 and is out of P06's `DELIVERABLES`). `calibration_id` stays
        `None` unless the caller names one, matching `run_start.payload.calibration_id`
        being nullable (PRD §9.5's derivation, `events/schema.py`).
        """
        return cls(
            calibration_id=calibration_id,
            defaults_ms={
                "llm_call": config.calibration_llm_ms,
                "tool_call": config.calibration_tool_ms,
                "agent_step": config.calibration_agent_step_ms,
            },
        )

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[CalibrationSample],
        *,
        config: SchedulerConfig,
        calibration_id: str,
        jitter: bool = False,
    ) -> CalibrationProfile:
        """Build a profile from a calibration recording pass (PRD §10.4 "Profile construction").

        Guarantees: every median and p90 is computed by integer nearest-rank over the sorted
        sample for its group — no `statistics.median` (which returns a float on an
        even-length input) and no `math.ceil` (which round-trips through a float). A group
        with a single sample has `median_ms == p90_ms == that sample`, which is correct
        nearest-rank behaviour, not a special case.
        """
        by_group_samples: dict[tuple[str, str, str], list[int]] = {}
        by_kind_samples: dict[str, list[int]] = {}
        for sample in samples:
            key = (sample.agent_id, sample.kind, sample.name)
            by_group_samples.setdefault(key, []).append(sample.duration_wall_ms)
            by_kind_samples.setdefault(sample.kind, []).append(sample.duration_wall_ms)
        return cls(
            calibration_id=calibration_id,
            by_group={k: _entry_from(v) for k, v in by_group_samples.items()},
            by_kind={k: _entry_from(v) for k, v in by_kind_samples.items()},
            defaults_ms={
                "llm_call": config.calibration_llm_ms,
                "tool_call": config.calibration_tool_ms,
                "agent_step": config.calibration_agent_step_ms,
            },
            jitter=jitter,
        )


def _entry_from(durations: list[int]) -> CalibrationEntry:
    """Return the `CalibrationEntry` for one group's sorted wall-clock durations."""
    ordered = sorted(durations)
    return CalibrationEntry(
        median_ms=_nearest_rank(ordered, 50),
        p90_ms=_nearest_rank(ordered, 90),
        samples=len(ordered),
    )


def _nearest_rank(ordered: list[int], percentile: int) -> int:
    """Return the nearest-rank `percentile`-th value of an already-sorted list.

    Integer-only nearest rank: `rank = ceil(percentile * n / 100)`, computed as
    `-(-percentile * n // 100)` (ceiling division via negation, never `math.ceil`), clamped
    into `[1, n]`. No float is constructed at any point.
    """
    n = len(ordered)
    rank = -(-percentile * n // 100)
    rank = min(max(rank, 1), n)
    return ordered[rank - 1]


def drift_permille(virtual_makespan_ms: int, expected_makespan_ms: int) -> int:
    """Return how far `virtual_makespan_ms` diverges from `expected_makespan_ms`, per-mille.

    Used by a future analysis pass (P10/P11) to implement PRD §10.4's drift check
    (">10% divergence raises `analysis_warning: clock_drift`") — that event type does not
    exist in the closed `EventType` enum (`events/schema.py`), so emitting it is out of this
    module's layer entirely; this function only computes the number, as an integer per-mille
    ratio (ADR-007: no floats in anything that could reach the log).

    Returns:
        `1000 * |virtual - expected| // expected`, or 0 when `expected_makespan_ms` is 0
        (nothing to diverge from).
    """
    if expected_makespan_ms == 0:
        return 0
    delta = abs(virtual_makespan_ms - expected_makespan_ms)
    return (1000 * delta) // expected_makespan_ms


__all__ = [
    "CALIBRATED_KINDS",
    "SPAN_KINDS",
    "CalibrationEntry",
    "CalibrationProfile",
    "CalibrationSample",
    "ClockError",
    "RandomSource",
    "VirtualClock",
    "drift_permille",
    "wall_time",
]
