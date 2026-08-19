r"""Per-fault and aggregate resilience scoring (PRD §19) — over sealed event logs only.

`agentdx.analysis.resilience` — `score(baseline, fault_runs)` (PRD §24.3's module map, matched
here exactly). "Baseline" in this module means PRD §19.1's **no-fault control run of the same
scenario** — a different thing from `analysis.baseline`'s single-agent baseline (§17). The two
modules do not import each other and the two "baseline" words are never used interchangeably in
this file; see the module docstring's naming note in `docs/baseline-methodology.md` §7.

**I3 purity.** Imports only `agentdx.events` and the standard library — no `runtime`, no `sdk`,
no `scenario` (fault weights and recovery budgets are accepted as plain parameters, the same
discipline `analysis.baseline` uses for `task`, since `scenario/` is not on `analysis/`'s
permitted-import list, CONTEXT.md §4).

## Every §19.1 input, checked against what the event schema actually carries

| Input | Source | Available? |
|---|---|---|
| `baseline_success`/`fault_success` | `assertion_result` (success_check, passed) | **Yes** |
| `recovery_time_virtual_ms` | Ruled below — see "Recovery time, operationalised" | Yes, ruled |
| `retries_base`/`retries_fault` | `span_start.attributes.retry_of` count | **Yes** |
| `degradation_class` | §19.5's four classes | **Partial — see below** |

## Recovery time, operationalised (a documented reading, not a stop)

PRD §19.1 defines `recovery_time_virtual_ms` as "virtual ms from `fault_injected` to the first
successful completion of **the affected subgraph**." No event type names "the affected
subgraph" as a first-class concept, but `Event.fault_id` does: `runtime/faults/taint.py`'s
`FaultTaintTracker` already stamps every event downstream of a fault with that fault's id
(PRD §9.4), and it is a real, persisted field on every event in this build's schema (`events/
schema.py`'s `Stamp.fault_id`) — not something this module has to recompute. **Recovery time
here is the virtual-ms gap from the fault's own `fault_injected` event to the first `span_end`
event carrying that fault's `fault_id` with `payload.status == "ok"`** — the first piece of
*tainted* work to finish successfully, which is exactly "the affected subgraph" resolved
mechanically rather than guessed at. If no such event exists (the run's own fault-tainted work
never finishes cleanly), recovery is `None` and scores `0` for that component (PRD §19.3,
"never recovering... scores 0").

## Degradation classification, ruled — one input is genuinely unavailable, not merely terse

PRD §19.5 needs a *system-emitted signal* to place a run into `degraded_flagged` ("succeeded
with reduced quality **and** the system emitted a signal — a warning, a low-confidence marker,
a fallback path taken") and to distinguish `graceful`'s "failed and reported the failure (a
surfaced error, a declared fallback, a partial result marked partial)" from `hard_failure`'s
plainer "failed loudly with a clear error." **No event type in this build's frozen schema
carries such a signal** — `instrumentation_gap` records a *capture* defect, not a task-quality
one, and `nondeterminism_warning`'s closed enum is about ambient non-determinism, not a
fallback or partial-result marker (`docs/event-schema.md`, checked exhaustively, not assumed).
This is the shape STOP CONDITION 3 names ("a resilience input is unavailable from the event
log") — but it disqualifies only *part* of one classifier, not the deliverable: `success_ratio`,
`recovery_component`, `amplification_component` and `silent_failure` detection (§19.5's own
distinguishing definition, "reported success while `success_check` failed," needs only
`run_end.status` plus `assertion_result`, both real) are all fully available and implemented in
full. Rather than block the whole module on the missing third of one input, or invent a fake
signal (guessing quietly is "the single most expensive failure mode," AGENTS.md §3), this
module makes the conservative, mechanically-available call and says so everywhere it matters:

- `passed` (succeeded): always `GRACEFUL`. `DEGRADED_FLAGGED` is **structurally unreachable** —
  `classify_degradation` never returns it, and `test_resilience.py::
  test_degraded_flagged_is_never_produced_by_this_build` asserts exactly that, so the gap is a
  visible, tested property rather than a silent one.
- Not `passed`, and `run_end.status == "complete"` (the system claimed success anyway): the
  literal §19.5 definition of `SILENT_FAILURE` — fully determined, no signal needed.
- Not `passed`, and `run_end.status != "complete"` (the system itself reported non-success):
  classified `HARD_FAILURE` rather than the higher-scored `GRACEFUL`, because this build cannot
  confirm the "declared fallback, partial result marked partial" condition `GRACEFUL`'s failed
  branch requires — the lower score is the safer direction for a number a user acts on
  (`redundancy.py`'s "over-disqualify rather than under-report" precedent, applied here to
  degradation instead of redundancy).

**Flagged for a PRD amendment, and in `docs/baseline-methodology.md`'s limitations section**:
either `degraded_flagged`/`graceful`-vs-`hard_failure` need a concrete event-schema signal (a
`fallback_taken`/`partial_result` marker), or §19.5 should say explicitly that a build without
one collapses to the conservative reading above.

PRD §19.1 (inputs), §19.2 (success ratio), §19.3 (recovery time), §19.4 (retry amplification),
§19.5 (degradation classification), §19.6 (per-fault score and aggregation), §19.7 (aggregation
rules — non-negotiable), §19.8 (example output).

**Determinism (NFR-14).** Every collection is a `tuple` in `fault_id` order or a `dict` built
in a fixed key sequence; no bare `set` iteration.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from agentdx.events.schema import Event, EventType

_DOCS: Final = "docs/baseline-methodology.md"
_RULES_FILENAME: Final = "verdict_rules.toml"
_EPSILON: Final = 1e-9


class DegradationClass(StrEnum):
    """PRD §19.5's four degradation classes.

    **`DEGRADED_FLAGGED` is declared but never produced by `classify_degradation`** in this
    build — see the module docstring's "Degradation classification, ruled". Kept as a member
    (rather than removed) so `DegradationWeights`/`verdict_rules.toml` and any future PRD-
    amendment-driven classifier upgrade have a stable name to target.
    """

    GRACEFUL = "graceful"
    DEGRADED_FLAGGED = "degraded_flagged"
    HARD_FAILURE = "hard_failure"
    SILENT_FAILURE = "silent_failure"


class FaultRunStatus(StrEnum):
    """Whether a `FaultRunInput` was actually scored (§19.7's three exclusion rules)."""

    SCORED = "scored"
    NOT_FIRED = "not_fired"
    ABORTED = "aborted"


class ResilienceAnalysisError(RuntimeError):
    """A hard resilience-scoring invariant failure — `E-RES-0NN`.

    Raised only when a genuinely required input is missing from a fault run's log (no
    `success_check` `assertion_result` at all — §19.2's `success_ratio` has nothing to divide),
    never for the documented `degraded_flagged` gap above, which is a classification choice,
    not a missing-data error.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


# ---------------------------------------------------------------------------------------------
# Reading events
# ---------------------------------------------------------------------------------------------


def _str_payload(event: Event, key: str) -> str | None:
    value = event.payload.get(key)
    return value if isinstance(value, str) else None


def _bool_payload(event: Event, key: str) -> bool | None:
    value = event.payload.get(key)
    return value if isinstance(value, bool) else None


def _attributes(event: Event) -> Mapping[str, object]:
    value = event.payload.get("attributes")
    return value if isinstance(value, Mapping) else {}


def _run_end(events: Sequence[Event]) -> Event | None:
    return next((e for e in events if e.type is EventType.RUN_END), None)


def _success_check_events(events: Sequence[Event]) -> tuple[Event, ...]:
    return tuple(
        e
        for e in events
        if e.type is EventType.ASSERTION_RESULT and _str_payload(e, "kind") == "success_check"
    )


def _success_rate(events: Sequence[Event]) -> float | None:
    """Return the fraction of `success_check` assertions in `events` that passed.

    `None` when `events` carries no `success_check` assertion at all (PRD §19.2's input is
    missing, not merely zero — a caller must treat this as "cannot score", not as a 0.0 rate).
    """
    checks = _success_check_events(events)
    if not checks:
        return None
    passed = sum(1 for e in checks if _bool_payload(e, "passed"))
    return passed / len(checks)


def _last_success_check_passed(events: Sequence[Event]) -> bool | None:
    checks = _success_check_events(events)
    if not checks:
        return None
    return bool(_bool_payload(max(checks, key=lambda e: e.seq), "passed"))


def _fault_fired(events: Sequence[Event], fault_id: str) -> bool:
    """Return whether `fault_id` actually fired in `events`.

    `fault_injected` is emitted **only the first time a fault fires** (`runtime/faults/
    {process,transport,dependency}.py`'s `_emit_fault_injected_if_first`; `runtime/faults/
    safety.py`: "no fault fires, no `fault_injected` event is ever written"). Its absence for
    a given `fault_id` is therefore a precise, direct signal that the fault never fired — not
    an inference.
    """
    return any(
        e.type is EventType.FAULT_INJECTED and _str_payload(e, "fault_id") == fault_id
        for e in events
    )


def _first_fault_injected(events: Sequence[Event], fault_id: str) -> Event | None:
    matches = [
        e
        for e in events
        if e.type is EventType.FAULT_INJECTED and _str_payload(e, "fault_id") == fault_id
    ]
    return min(matches, key=lambda e: e.seq) if matches else None


def _recovery_time_virtual_ms(
    events: Sequence[Event], fault_id: str, fault_injected: Event
) -> int | None:
    """Return PRD §19.3's recovery time, per the module docstring's "Recovery time" ruling."""
    candidates = [
        e
        for e in events
        if e.type is EventType.SPAN_END
        and e.fault_id == fault_id
        and _str_payload(e, "status") == "ok"
        and e.seq > fault_injected.seq
    ]
    if not candidates:
        return None
    first = min(candidates, key=lambda e: e.seq)
    return max(0, first.virtual_ts_ms - fault_injected.virtual_ts_ms)


def _retry_count(events: Sequence[Event]) -> int:
    """Return the number of spans with `attributes.retry_of` set — matches `timing.py`'s field."""
    return sum(
        1
        for e in events
        if e.type is EventType.SPAN_START and _str_payload_attr(e, "retry_of") is not None
    )


def _str_payload_attr(event: Event, key: str) -> str | None:
    value = _attributes(event).get(key)
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------------------------
# PRD §19.5 — degradation classification
# ---------------------------------------------------------------------------------------------


def classify_degradation(*, success_check_passed: bool, run_end_status: str) -> DegradationClass:
    """Return PRD §19.5's degradation class — see the module docstring's ruling.

    `DEGRADED_FLAGGED` is never returned by this function (declared, tested gap).
    """
    if success_check_passed:
        return DegradationClass.GRACEFUL
    if run_end_status == "complete":
        return DegradationClass.SILENT_FAILURE
    return DegradationClass.HARD_FAILURE


# ---------------------------------------------------------------------------------------------
# Config — verdict_rules.toml's [resilience] table
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResilienceRules:
    """`verdict_rules.toml`'s `[resilience]`/`[resilience.degradation_weights]` tables, typed."""

    recovery_budget_multiplier: float
    amplification_budget: float
    success_ratio_weight: float
    recovery_weight: float
    amplification_weight: float
    degradation_weight: float
    silent_failure_cap: int
    degradation_weights: Mapping[DegradationClass, float]


_DEFAULT_RULES: Final = ResilienceRules(
    recovery_budget_multiplier=2.0,
    amplification_budget=4.0,
    success_ratio_weight=0.50,
    recovery_weight=0.20,
    amplification_weight=0.15,
    degradation_weight=0.15,
    silent_failure_cap=49,
    degradation_weights={
        DegradationClass.GRACEFUL: 1.0,
        DegradationClass.DEGRADED_FLAGGED: 0.6,
        DegradationClass.HARD_FAILURE: 0.4,
        DegradationClass.SILENT_FAILURE: 0.0,
    },
)


def load_resilience_rules() -> ResilienceRules:
    """Return `verdict_rules.toml`'s `[resilience]` tables, typed.

    Falls back to the PRD §19.3/§19.4/§19.6 defaults only if the file cannot be found — a
    condition that only arises in a test run outside the repository layout, never in
    production, where `verdict_rules.toml` is always committed beside this module.
    """
    for parent in Path(__file__).resolve().parents:
        toml_path = parent / _RULES_FILENAME
        if toml_path.is_file():
            return _parse_rules(tomllib.loads(toml_path.read_text(encoding="utf-8")))
        candidate = parent.parent / "src" / "agentdx" / "analysis" / _RULES_FILENAME
        if candidate.is_file():
            return _parse_rules(tomllib.loads(candidate.read_text(encoding="utf-8")))
    return _DEFAULT_RULES


def _parse_rules(data: Mapping[str, object]) -> ResilienceRules:
    section = data.get("resilience", {})
    if not isinstance(section, dict):
        return _DEFAULT_RULES
    weights_section = section.get("degradation_weights", {})
    weights = dict(_DEFAULT_RULES.degradation_weights)
    if isinstance(weights_section, dict):
        for key, default in _DEFAULT_RULES.degradation_weights.items():
            value = weights_section.get(key.value)
            weights[key] = float(value) if isinstance(value, int | float) else default
    return ResilienceRules(
        recovery_budget_multiplier=float(
            section.get("recovery_budget_multiplier", _DEFAULT_RULES.recovery_budget_multiplier)
        ),
        amplification_budget=float(
            section.get("amplification_budget", _DEFAULT_RULES.amplification_budget)
        ),
        success_ratio_weight=float(
            section.get("success_ratio_weight", _DEFAULT_RULES.success_ratio_weight)
        ),
        recovery_weight=float(section.get("recovery_weight", _DEFAULT_RULES.recovery_weight)),
        amplification_weight=float(
            section.get("amplification_weight", _DEFAULT_RULES.amplification_weight)
        ),
        degradation_weight=float(
            section.get("degradation_weight", _DEFAULT_RULES.degradation_weight)
        ),
        silent_failure_cap=int(
            section.get("silent_failure_cap", _DEFAULT_RULES.silent_failure_cap)
        ),
        degradation_weights=weights,
    )


# ---------------------------------------------------------------------------------------------
# Per-fault scoring
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FaultRunInput:
    """One fault scenario's execution, ready to be scored (PRD §19's "per fault scenario").

    `fault_id` is the id the run's own `fault_injected` events carry (used to check whether the
    fault actually fired — §19.7.3 — and to compute recovery time via `Event.fault_id`
    taint). `fault_label` is the human-readable name for the §19.8 table (e.g.
    `"agent_crash(reviewer)"`); `recovery_budget_ms`/`weight` override `verdict_rules.toml`'s
    defaults for this one fault (PRD §19.3's `scenario.recovery_budget_ms`, §19.6's
    `scenario.fault_weights` — both scenario-declared, so accepted as plain parameters here per
    the module docstring's I3 note).
    """

    fault_id: str
    fault_label: str
    events: tuple[Event, ...]
    recovery_budget_ms: int | None = None
    weight: float | None = None


@dataclass(frozen=True, slots=True)
class FaultScore:
    """PRD §19.6's per-fault score, plus every input that produced it (I6).

    Guarantees: `status is FaultRunStatus.SCORED` iff `score` is not `None`; `evidence_seq` is
    non-empty whenever `status is SCORED` (I6 — an aggregate-affecting number always traces to
    concrete `seq` values).
    """

    fault_id: str
    fault_label: str
    status: FaultRunStatus
    success_ratio: float | None
    recovery_component: float | None
    recovery_time_virtual_ms: int | None
    amplification: float | None
    amplification_component: float | None
    retries_base: int
    retries_fault: int
    degradation_class: DegradationClass | None
    score: float | None
    evidence_seq: tuple[int, ...]


def _score_one_fault(
    baseline_events: Sequence[Event],
    baseline_success: float,
    fault_run: FaultRunInput,
    rules: ResilienceRules,
) -> FaultScore:
    run_end = _run_end(fault_run.events)
    run_end_status = _str_payload(run_end, "status") if run_end is not None else None

    if not _fault_fired(fault_run.events, fault_run.fault_id):
        return FaultScore(
            fault_id=fault_run.fault_id,
            fault_label=fault_run.fault_label,
            status=FaultRunStatus.NOT_FIRED,
            success_ratio=None,
            recovery_component=None,
            recovery_time_virtual_ms=None,
            amplification=None,
            amplification_component=None,
            retries_base=_retry_count(baseline_events),
            retries_fault=_retry_count(fault_run.events),
            degradation_class=None,
            score=None,
            evidence_seq=(),
        )

    if run_end_status == "aborted_guard":
        evidence = tuple(sorted(e.seq for e in fault_run.events if e.type is EventType.RUN_END))
        return FaultScore(
            fault_id=fault_run.fault_id,
            fault_label=fault_run.fault_label,
            status=FaultRunStatus.ABORTED,
            success_ratio=None,
            recovery_component=None,
            recovery_time_virtual_ms=None,
            amplification=None,
            amplification_component=None,
            retries_base=_retry_count(baseline_events),
            retries_fault=_retry_count(fault_run.events),
            degradation_class=None,
            score=None,
            evidence_seq=evidence,
        )

    fault_success = _success_rate(fault_run.events)
    if fault_success is None:
        raise ResilienceAnalysisError(
            "E-RES-001",
            f"fault run {fault_run.fault_label!r} has no success_check assertion_result — "
            "PRD §19.2's success_ratio has nothing to divide",
        )
    passed = _last_success_check_passed(fault_run.events)
    if passed is None or run_end_status is None:
        raise ResilienceAnalysisError(
            "E-RES-001",
            f"fault run {fault_run.fault_label!r} is missing a success_check result or "
            "run_end.status — cannot classify degradation (PRD §19.5)",
        )

    success_ratio = max(0.0, min(1.0, fault_success / max(baseline_success, _EPSILON)))

    fault_injected = _first_fault_injected(fault_run.events, fault_run.fault_id)
    assert fault_injected is not None  # noqa: S101 — guaranteed by the _fault_fired check above
    recovery_time = _recovery_time_virtual_ms(fault_run.events, fault_run.fault_id, fault_injected)

    baseline_end = _run_end(baseline_events)
    baseline_makespan = (
        _int_payload(baseline_end, "virtual_makespan_ms") if baseline_end is not None else None
    ) or 0
    budget = fault_run.recovery_budget_ms
    if budget is None:
        budget = max(1, round(rules.recovery_budget_multiplier * baseline_makespan))
    recovery_component = (
        max(0.0, min(1.0, 1 - recovery_time / budget)) if recovery_time is not None else 0.0
    )

    retries_base = _retry_count(baseline_events)
    retries_fault = _retry_count(fault_run.events)
    amplification = retries_fault / max(retries_base, 1)
    amplification_component = max(
        0.0, min(1.0, 1 - (amplification - 1) / rules.amplification_budget)
    )

    degradation_class = classify_degradation(
        success_check_passed=passed, run_end_status=run_end_status
    )
    degradation_weight = rules.degradation_weights[degradation_class]

    score = 100 * (
        rules.success_ratio_weight * success_ratio
        + rules.recovery_weight * recovery_component
        + rules.amplification_weight * amplification_component
        + rules.degradation_weight * degradation_weight
    )

    evidence_seqs = [fault_injected.seq]
    evidence_seqs.extend(e.seq for e in _success_check_events(fault_run.events))
    if run_end is not None:
        evidence_seqs.append(run_end.seq)
    evidence = tuple(sorted(set(evidence_seqs)))

    return FaultScore(
        fault_id=fault_run.fault_id,
        fault_label=fault_run.fault_label,
        status=FaultRunStatus.SCORED,
        success_ratio=success_ratio,
        recovery_component=recovery_component,
        recovery_time_virtual_ms=recovery_time,
        amplification=amplification,
        amplification_component=amplification_component,
        retries_base=retries_base,
        retries_fault=retries_fault,
        degradation_class=degradation_class,
        score=score,
        evidence_seq=evidence,
    )


def _int_payload(event: Event, key: str) -> int | None:
    value = event.payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


# ---------------------------------------------------------------------------------------------
# PRD §19.6/§19.7 — aggregation
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResilienceResult:
    """PRD §19.6-§19.8's aggregate result, always carrying its own per-fault breakdown (§19.7.1).

    Guarantees: `per_fault` has exactly one entry per input `FaultRunInput`, in input order —
    the aggregate **never appears without it**, structurally (there is no code path that
    constructs a `ResilienceResult` without populating `per_fault`, and every field access goes
    through this one dataclass). `resilience_score` and `worst_fault_score` are `None` when zero
    faults were scored (all excluded, or `fault_runs` was empty) — never fabricated as `0`, `100`
    or any other placeholder (AGENTS.md §2: no stub presented as done). `silent_failure_capped`
    is `True` iff rule §19.7.4 actually reduced the aggregate (a hard, tested rule — see
    `test_resilience.py::test_a_single_silent_failure_caps_the_aggregate_at_49`).
    """

    resilience_score: int | None
    worst_fault_score: int | None
    n_faults: int
    per_fault: tuple[FaultScore, ...]
    not_fired: tuple[str, ...]
    aborted: tuple[str, ...]
    silent_failure_capped: bool
    evidence_seq: tuple[int, ...]


def score(
    baseline_events: Sequence[Event],
    fault_runs: Sequence[FaultRunInput],
    *,
    fault_weights: Mapping[str, float] | None = None,
    rules: ResilienceRules | None = None,
) -> ResilienceResult:
    """Score `fault_runs` against `baseline_events`.

    PRD §19 — the module map's `score(baseline, fault_runs)`.

    Args:
        baseline_events: The no-fault control run of the same scenario (§19.1's
            `baseline_success` source — **not** `analysis.baseline`'s single-agent baseline;
            see the module docstring's naming note).
        fault_runs: One `FaultRunInput` per fault scenario.
        fault_weights: `{fault_id: weight}` for `weighted_mean` (§19.6) — overrides each
            `FaultRunInput.weight`; `None` uses each input's own `weight`, or equal weighting
            if neither is set (PRD §19.6: "or equal").
        rules: Override for `load_resilience_rules()`'s defaults (mainly for tests).

    Returns:
        The `ResilienceResult`.

    Raises:
        ResilienceAnalysisError: `E-RES-001` — `baseline_events` has no `success_check`
            assertion (§19.2 has nothing to divide by), or a *fired, non-aborted* fault run is
            missing a `success_check` result or `run_end.status`.
    """
    active_rules = rules if rules is not None else load_resilience_rules()

    baseline_success = _success_rate(baseline_events)
    if baseline_success is None:
        raise ResilienceAnalysisError(
            "E-RES-001",
            "baseline_events has no success_check assertion_result — PRD §19.2's "
            "success_ratio has nothing to divide",
        )
    if baseline_success == 0.0:
        raise ResilienceAnalysisError(
            "E-RES-001",
            "baseline_success == 0 — the experiment is invalid (PRD §19.2: abort at the "
            "steady-state check rather than dividing)",
        )

    per_fault: list[FaultScore] = []
    for fault_run in fault_runs:
        per_fault.append(
            _score_one_fault(baseline_events, baseline_success, fault_run, active_rules)
        )

    scored = [f for f in per_fault if f.status is FaultRunStatus.SCORED]
    not_fired = tuple(f.fault_id for f in per_fault if f.status is FaultRunStatus.NOT_FIRED)
    aborted = tuple(f.fault_id for f in per_fault if f.status is FaultRunStatus.ABORTED)

    if not scored:
        evidence = tuple(sorted({s for f in per_fault for s in f.evidence_seq}))
        return ResilienceResult(
            resilience_score=None,
            worst_fault_score=None,
            n_faults=0,
            per_fault=tuple(per_fault),
            not_fired=not_fired,
            aborted=aborted,
            silent_failure_capped=False,
            evidence_seq=evidence,
        )

    weights: dict[str, float] = {}
    for f in scored:
        resolved: float | None = None
        if fault_weights is not None:
            resolved = fault_weights.get(f.fault_id)
        if resolved is None:
            source = next(fr for fr in fault_runs if fr.fault_id == f.fault_id)
            resolved = source.weight
        weights[f.fault_id] = resolved if resolved is not None else 1.0

    total_weight = sum(weights[f.fault_id] for f in scored)
    # Every default weight is 1.0; a caller supplying all-zero weights would divide by zero,
    # deliberately not defended further, since a scenario declaring every fault_weight as 0 is
    # a configuration error the caller must fix, not silently paper over.
    assert total_weight > 0  # noqa: S101
    aggregate = sum(weights[f.fault_id] * (f.score or 0.0) for f in scored) / total_weight

    capped = any(f.degradation_class is DegradationClass.SILENT_FAILURE for f in scored)
    if capped:
        aggregate = min(aggregate, float(active_rules.silent_failure_cap))

    worst = min(f.score or 0.0 for f in scored)
    evidence = tuple(sorted({s for f in per_fault for s in f.evidence_seq}))

    return ResilienceResult(
        resilience_score=round(aggregate),
        worst_fault_score=round(worst),
        n_faults=len(scored),
        per_fault=tuple(per_fault),
        not_fired=not_fired,
        aborted=aborted,
        silent_failure_capped=capped,
        evidence_seq=evidence,
    )


# ---------------------------------------------------------------------------------------------
# PRD §19.8 — example-shaped table output
# ---------------------------------------------------------------------------------------------


def format_resilience_table(result: ResilienceResult) -> str:
    """Render `result` as PRD §19.8's table shape.

    Deterministic: `per_fault` prints in its already-fixed input order; `not_fired`/`aborted`
    always print, even when empty.
    """
    header_score = "n/a" if result.resilience_score is None else str(result.resilience_score)
    worst = "n/a" if result.worst_fault_score is None else str(result.worst_fault_score)
    header = f"Resilience: {header_score} / 100        ({result.n_faults} faults, worst {worst})"
    lines = [header, ""]
    lines.append(
        f"  {'fault':<30} {'success':>7}  {'recovery':>9}  {'retry-amp':>9}  "
        f"{'degradation':>12}  {'score':>5}"
    )
    lines.append("  " + "─" * 75)
    for f in result.per_fault:
        if f.status is not FaultRunStatus.SCORED:
            lines.append(f"  {f.fault_label:<30} [{f.status.value}]")
            continue
        success = f"{f.success_ratio:.2f}" if f.success_ratio is not None else "n/a"
        recovery = (
            f"{f.recovery_time_virtual_ms}ms" if f.recovery_time_virtual_ms is not None else "n/a"
        )
        amp = f"{f.amplification:.1f}×" if f.amplification is not None else "n/a"
        degradation = f.degradation_class.value if f.degradation_class is not None else "n/a"
        score_str = f"{f.score:.0f}" if f.score is not None else "n/a"
        lines.append(
            f"  {f.fault_label:<30} {success:>7}  {recovery:>9}  {amp:>9}  "
            f"{degradation:>12}  {score_str:>5}"
        )
    lines.append("")
    not_fired = ", ".join(result.not_fired) if result.not_fired else "none"
    aborted = ", ".join(result.aborted) if result.aborted else "none"
    lines.append(f"  not fired: {not_fired}      aborted: {aborted}")
    if result.silent_failure_capped:
        lines.append(
            f"  ⚠ a silent_failure was observed — aggregate capped at "
            f"{load_resilience_rules().silent_failure_cap} (PRD §19.7.4)"
        )
    return "\n".join(lines)


__all__ = [
    "DegradationClass",
    "FaultRunInput",
    "FaultRunStatus",
    "FaultScore",
    "ResilienceAnalysisError",
    "ResilienceResult",
    "ResilienceRules",
    "classify_degradation",
    "format_resilience_table",
    "load_resilience_rules",
    "score",
]
