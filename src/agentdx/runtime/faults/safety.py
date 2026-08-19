"""Chaos safety architecture (PRD §13): blast-radius re-checks, steady-state hypothesis, guards.

Read this module's docstrings before any fault-class execution module — every one of them
calls into this file before applying an effect, never the other way round.

**Why safety is built before faults, structurally, not just chronologically (mission Design
Constraint 1).** `reauthorize` is not a wrapper an execution module might forget to call — it
is the only path `runtime.faults.process`/`transport`/`dependency` have to convert an
`ArmedFault` + a live target into permission to act; none of them import `registry.BlastRadius`
directly for their own re-checks; all of them call this module's `reauthorize`. If a future
fault-class module skipped it, that is a code-review-visible omission (no call to
`safety.reauthorize` anywhere in the module), not a silently-widened blast radius.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from agentdx.scenario.schema import TargetKind, parse_comparison

if TYPE_CHECKING:
    from agentdx.runtime.faults.registry import ArmedFault, BlastRadius

_DOCS: Final = "docs/chaos-safety.md"


class ChaosAuthorizationError(RuntimeError):
    """A fault tried to fire against a target outside its blast radius (PRD §13.4 point 2).

    Carries `E-CHAOS-001` (PRD §36: "Fault `tool_failure(deploy)` outside declared blast
    radius" — "Abort run"). This is the **defence-in-depth** check: `registry.py` already
    refuses to arm a fault against an out-of-radius target, so reaching this exception means
    either a fault's target resolves differently at fire time than at arm time (e.g. a glob
    match against a state key computed differently), or a defect in `registry.py` — PRD §13.4
    calls this check "a defence-in-depth check that should be unreachable", not a decoration.
    """

    code: Final = "E-CHAOS-001"

    def __init__(self, fault_type: str, target_kind: TargetKind, target: str) -> None:
        """Build the error naming the fault, its target kind and the target value."""
        super().__init__(
            f"[{self.code}] fault `{fault_type}({target})` outside declared blast radius "
            f"(target_kind={target_kind.value}) — aborting run ({_DOCS}#e-chaos-001)."
        )


def reauthorize(armed: ArmedFault, blast_radius: BlastRadius) -> None:
    """Re-check `armed`'s target against `blast_radius` immediately before applying an effect.

    PRD §13.4 point 2: "Enforced at two layers: validation ... and runtime (`should_fire`
    re-checks; a violation raises `E-CHAOS-001` and aborts the run — a defence-in-depth check
    that should be unreachable)." Every fault-class execution module calls this, every time,
    right before it mutates anything observable (crashing a task, dropping a message, failing
    a tool call) — never once at construction and then trusted for the fault's whole life.

    Raises:
        ChaosAuthorizationError: `armed`'s target is not in `blast_radius` (`E-CHAOS-001`).
    """
    decl = armed.decl
    if not blast_radius.contains(decl.target_kind, decl.target):
        raise ChaosAuthorizationError(decl.fault_type, decl.target_kind, decl.target)


# ---------------------------------------------------------------------------------------
# Steady-state hypothesis (PRD §13.5)
# ---------------------------------------------------------------------------------------


class AbortPrecondition(RuntimeError):
    """The baseline phase violated the steady-state hypothesis (PRD §12.4, §13.5).

    "You cannot measure deviation from a steady state you never had." Raised by
    `SteadyStateHypothesis.check` when called against baseline-phase metrics; the caller (the
    scenario execution lifecycle, PRD §12.4) aborts the whole experiment before faults are
    ever armed — no fault fires, no `fault_injected` event is ever written, because there was
    nothing valid to compare a faulted run against.
    """


@dataclass(frozen=True, slots=True)
class SteadyStateHypothesis:
    """The resolved `hypothesis:` section (PRD §13.5) — comparisons only, no execution.

    Each field is a raw `"<op> <value>"` string (PRD §21.1's own surface form, e.g.
    `">= 0.9"`) or `None` if the scenario did not declare that metric. Parsed lazily by
    `check`/`violations`, via `scenario.schema.parse_comparison` — the same parser
    `scenario.validate` already uses for `hypothesis:`/assertion comparisons, not a second one.
    """

    task_success: str | None = None
    p95_virtual_duration_ms: str | None = None
    max_token_spend: str | None = None

    @classmethod
    def from_resolved_scenario(cls, resolved: Mapping[str, object]) -> SteadyStateHypothesis:
        """Build from a resolved scenario document's `hypothesis:` section (may be absent)."""
        raw = resolved.get("hypothesis")
        section: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
        return cls(
            task_success=_as_str(section.get("task_success")),
            p95_virtual_duration_ms=_as_str(section.get("p95_virtual_duration_ms")),
            max_token_spend=_as_str(section.get("max_token_spend")),
        )

    def violations(self, metrics: Mapping[str, float]) -> tuple[str, ...]:
        """Return one message per declared metric whose comparison `metrics` fails.

        A metric declared in the hypothesis but missing from `metrics` counts as a violation
        (PRD §13.5 gives no "metric not measured, skip the check" escape hatch — the baseline
        phase is expected to produce every metric the hypothesis names). A metric present in
        `metrics` but not declared in the hypothesis is ignored (nothing to check it against).
        """
        out: list[str] = []
        for field_name, expr in (
            ("task_success", self.task_success),
            ("p95_virtual_duration_ms", self.p95_virtual_duration_ms),
            ("max_token_spend", self.max_token_spend),
        ):
            if expr is None:
                continue
            comparison = parse_comparison(expr)
            if comparison is None:
                out.append(f"{field_name}: malformed comparison {expr!r}")
                continue
            if field_name not in metrics:
                out.append(f"{field_name}: hypothesis declares it but it was not measured")
                continue
            if not comparison.evaluate(metrics[field_name]):
                out.append(f"{field_name}={metrics[field_name]!r} fails `{comparison}`")
        return tuple(out)

    def check(self, metrics: Mapping[str, float], *, phase: str) -> None:
        """Raise `AbortPrecondition` if any declared metric violates its comparison.

        Args:
            metrics: The measured values for this phase (PRD §13.5: baseline-phase metrics
                for the precondition check, fault-phase metrics for the post-hoc delta).
            phase: `"baseline"` or `"fault"` — named in the raised error only, changes no
                behaviour (both phases use the same comparisons; only baseline-phase failures
                are `ABORT_PRECONDITION` per PRD §12.4's lifecycle table, but that distinction
                is the caller's to make, not this method's — it always raises on a violation).

        Raises:
            AbortPrecondition: at least one declared metric fails its comparison.
        """
        bad = self.violations(metrics)
        if bad:
            joined = "; ".join(bad)
            detail = (
                f"steady-state hypothesis violated in {phase} phase: {joined} "
                f"({_DOCS}#steady-state-hypothesis)"
            )
            raise AbortPrecondition(detail)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------------------
# Abort guards (PRD §13.6)
# ---------------------------------------------------------------------------------------

DEFAULT_GUARD_EVAL_STEP_INTERVAL: Final = 100
"""PRD §13.6: `max_wall_duration_s` is "Evaluated ... Every 100 steps". Not a magic number in
the sense AGENTS.md §4 forbids (a threshold that could silently drift) — it is PRD-specified
prose, not a tunable, and is named here once rather than as a bare literal at each call site."""


class AbortGuardTripped(RuntimeError):
    """One of PRD §13.6's abort guards tripped (`E-GUARD-001`).

    Raising this is how a `FaultInjectorHook` override signals "stop the run now" from inside
    `pre_schedule` — the only interception point this build has continuous access to (see
    `AbortGuardMonitor`'s class docstring). Declared gap: `runtime.scheduler.RunState.
    ABORTED_GUARD` exists as a legal lifecycle target from `RUNNING` (`scheduler.py`'s own
    `_LEGAL_TRANSITIONS`), but nothing in the fixed scheduler transitions to it — raising here
    propagates through `Scheduler.run()`'s existing `except BaseException` handler, which
    moves the run to `FAILED`, not `ABORTED_GUARD`. Reaching `ABORTED_GUARD` specifically would
    need a second, dedicated scheduler.py touch (a public abort method, or a hook return value
    the scheduler interprets) beyond the one narrow, justified addition this prompt already
    makes (`fault_id_for`) — judged out of scope here and recorded as NOT DONE rather than
    guessed at with a second unreviewed scheduler change in the same prompt. The partial event
    log is retained regardless (every event up to the trip was already written and flushed —
    NFR-13 holds), so "analysable partial log" is satisfied even though the terminal `RunState`
    value is not PRD-exact.
    """

    def __init__(self, trip: GuardTrip) -> None:
        """Build the error from the `GuardTrip` that caused it."""
        self.trip = trip
        super().__init__(str(trip))


@dataclass(frozen=True, slots=True)
class GuardTrip:
    """One abort-guard trip (PRD §13.6, §36 `E-GUARD-001`)."""

    guard: str
    detail: str

    def __str__(self) -> str:
        """Render PRD §36's own message shape, e.g. "Aborted: token budget 200 000 exceeded"."""
        return f"[E-GUARD-001] {self.guard}: {self.detail} ({_DOCS}#e-guard-001)"


@dataclass
class AbortGuardMonitor:
    """Continuous evaluation of PRD §13.6's six abort guards.

    **Continuously, not just at injection time (mission Design Constraint 5).** Every
    `observe_*` method is a pure check-and-report: it never aborts anything itself (this
    module raises no exception for a guard trip — a caller decides how to seal the run, since
    "the partial log is retained and analysable" (PRD §13.6) is a `store`/scheduler-level
    concern this module has no access to). Call one every time the corresponding PRD §13.6
    "Evaluated" column says to; the first non-`None` return is the trip to act on.

    **Wiring gap, declared (see `docs/chaos-safety.md` §"Abort guard wiring" and the closing
    NOT DONE block).** `runtime.scheduler.FaultInjectorHook` — fixed for this prompt — exposes
    `pre_schedule(step, runnable)` (every scheduler step: wires `observe_step` fully),
    `pre_yield(task_id, reason)` and `on_task_done(task_id, exception)`, but no interception
    point for an `llm_call`, a retry span, or a write-batch. `observe_llm_call`/`observe_retry`/
    `observe_event_batch` are therefore fully implemented, pure, and unit-tested against
    hand-built call sequences, but nothing in this build's real scheduler run calls them yet —
    the same structural gap PRD §12.1's `pre_llm` interception point has for every MVP fault
    that would use it (see `dependency.py`'s module docstring). `max_virtual_duration_ms` and
    `max_wall_duration_s` (the two guards `pre_schedule` genuinely reaches) are the two this
    build enforces live.
    """

    max_virtual_duration_ms: int
    max_tokens: int
    max_retries: int
    max_wall_duration_s: int
    max_events: int
    max_llm_calls: int

    _token_count: int = field(default=0, repr=False)
    _retry_count: int = field(default=0, repr=False)
    _event_count: int = field(default=0, repr=False)
    _llm_call_count: int = field(default=0, repr=False)

    @classmethod
    def from_resolved_guards(cls, guards: Mapping[str, object]) -> AbortGuardMonitor:
        """Build from a resolved scenario's `guards:` section.

        PRD §21.4 defaults already merged in by `scenario.loader.resolve_defaults` — this
        constructor trusts the caller resolved it, the same trust boundary `registry.
        FaultRegistry` extends to `blast_radius` and `chaos_opt_in`.
        """
        return cls(
            max_virtual_duration_ms=_as_int(guards.get("max_virtual_duration_ms")),
            max_tokens=_as_int(guards.get("max_tokens")),
            max_retries=_as_int(guards.get("max_retries")),
            max_wall_duration_s=_as_int(guards.get("max_wall_duration_s")),
            max_events=_as_int(guards.get("max_events")),
            max_llm_calls=_as_int(guards.get("max_llm_calls")),
        )

    def observe_step(
        self, *, step: int, virtual_ts_ms: int, wall_elapsed_ms: int
    ) -> GuardTrip | None:
        """Check `max_virtual_duration_ms` (every step) and `max_wall_duration_s` (every 100).

        Call once per scheduler step (from a `FaultInjectorHook.pre_schedule` override).
        """
        if virtual_ts_ms > self.max_virtual_duration_ms:
            return GuardTrip(
                "max_virtual_duration_ms",
                f"virtual duration {virtual_ts_ms}ms exceeded budget "
                f"{self.max_virtual_duration_ms}ms at step {step}",
            )
        if step % DEFAULT_GUARD_EVAL_STEP_INTERVAL == 0:
            wall_elapsed_s = wall_elapsed_ms // 1000
            if wall_elapsed_s > self.max_wall_duration_s:
                return GuardTrip(
                    "max_wall_duration_s",
                    f"wall duration {wall_elapsed_s}s exceeded budget "
                    f"{self.max_wall_duration_s}s at step {step}",
                )
        return None

    def observe_llm_call(self, *, prompt_tokens: int, completion_tokens: int) -> GuardTrip | None:
        """Check `max_llm_calls` and `max_tokens` (PRD §13.6: "Every `llm_call`").

        See the class docstring's wiring-gap note — call this from wherever `llm_call` events
        are produced once that call site exists; nothing in this build's scheduler loop calls
        it automatically yet.
        """
        self._llm_call_count += 1
        self._token_count += prompt_tokens + completion_tokens
        if self._llm_call_count > self.max_llm_calls:
            return GuardTrip(
                "max_llm_calls",
                f"call count {self._llm_call_count} exceeded budget {self.max_llm_calls}",
            )
        if self._token_count > self.max_tokens:
            return GuardTrip(
                "max_tokens",
                f"token budget {self.max_tokens} exceeded at {self._token_count}",
            )
        return None

    def observe_retry(self) -> GuardTrip | None:
        """Check `max_retries` (PRD §13.6: "Every retry span"). Same wiring-gap note applies."""
        self._retry_count += 1
        if self._retry_count > self.max_retries:
            return GuardTrip(
                "max_retries",
                f"retry count {self._retry_count} exceeded budget {self.max_retries}",
            )
        return None

    def observe_event_batch(self, batch_size: int) -> GuardTrip | None:
        """Check `max_events` (PRD §13.6: "Every write batch"). Same wiring-gap note applies."""
        self._event_count += batch_size
        if self._event_count > self.max_events:
            return GuardTrip(
                "max_events",
                f"event count {self._event_count} exceeded budget {self.max_events}",
            )
        return None


class MalformedGuardError(RuntimeError):
    """A resolved scenario's `guards:` section carried a non-integer value.

    Defensive only — `scenario.validate`'s `E-SCEN-008` already type/range-checks every guard
    key before a scenario resolves, so a correctly-validated scenario never reaches this.
    """


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"guard value must be an int, got {value!r} ({_DOCS}#malformed-guard)"
        raise MalformedGuardError(msg)
    return value


__all__ = [
    "DEFAULT_GUARD_EVAL_STEP_INTERVAL",
    "AbortGuardMonitor",
    "AbortGuardTripped",
    "AbortPrecondition",
    "ChaosAuthorizationError",
    "GuardTrip",
    "MalformedGuardError",
    "SteadyStateHypothesis",
    "reauthorize",
]
