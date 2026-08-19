r"""The verdict engine (PRD §18) — class, score, confidence and recommendations.

> PRD §18: "The verdict must never be a black-box LLM opinion. Prefer deterministic
> calculations wherever possible. All rules below are pure functions of analysis outputs."

Taken literally: `verdict()` takes **already-computed analysis outputs** — `baseline.
BaselineComparison`, `overhead.OverheadDecomposition`/`TotalWorkDecomposition`, `resilience.
ResilienceResult`, `aggregates.EdgeAggregate`/`AgentAggregate`, `redundancy.RedundancyGroup` —
never raw events, and never recomputes anything a sibling module already owns. This is not a
style choice: PRD §24.3's module map lists `agentdx.analysis.verdict`'s one dependency as "all
analysers," and a pure function of their outputs is exactly what a pure function of outputs
looks like in code.

## Design Constraint 3, mechanised in the type (I6)

`Evidence.__post_init__` raises `EmptyEvidenceError` the instant an `Evidence` is constructed
with an empty `event_seqs` tuple. Every `VerdictFinding`/`Recommendation` embeds a required
(non-`Optional`) `Evidence` field, so there is no code path — not a missed `if`, not a review
oversight — that can produce a finding or recommendation with no evidence. This is the literal
reading of the mission brief's "structurally impossible to render": the type cannot hold an
empty array, so nothing downstream ever receives one. `tests/analysis/test_verdict.py::
test_an_empty_evidence_array_is_rejected_by_the_type_itself` demonstrates it.

## `analysis.race` does not exist yet (P12) — `state_conflict_findings` is an open seam, not a gap

PRD §18.1's `STATE_CONFLICT_RISK` class and §18.2's `conflict_penalty` both need `state_conflict`
findings, which only `analysis.race` (P12, `NOT STARTED` per CONTEXT.md §5 row 12) can produce.
`verdict()` accepts them as `state_conflict_findings: Sequence[StateConflictFinding] = ()` — a
`Protocol` matching the exact shape `scenario.assertions.Finding` already declares (`type`,
`severity`, `evidence_seq`), so the day P12 ships a real `Finding` type, no signature here needs
to change. Called with the default empty sequence (every test and every real caller until P12
exists), `STATE_CONFLICT_RISK` structurally never fires and `conflict_penalty` is always `0` —
declared, not hidden, and covered by `test_verdict.py::
test_state_conflict_risk_never_fires_without_findings`.

PRD §18.1 (verdict classes and precedence), §18.2 (scoring formula), §18.3 (severity), §18.4
(the evidence contract), §18.5 (confidence), §18.6 (recommendations).

**I3 purity.** Imports only sibling `analysis.*` modules (`baseline`, `overhead`, `redundancy`,
`aggregates`, `resilience`, `timing`), `agentdx.events` (`Event` type hints only), and the
standard library.

**Determinism (NFR-14).** Findings and recommendations are always built by iterating an already-
sorted input (`edge_aggregates`/`agent_aggregates`/`redundancy_groups` are already sorted by
their producing modules) in a fixed rule order; no bare `set` iteration.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from agentdx.analysis.aggregates import AgentAggregate, EdgeAggregate
from agentdx.analysis.baseline import BaselineComparison, BaselineOutcome, ComparabilityGrade
from agentdx.analysis.overhead import OverheadDecomposition, TotalWorkDecomposition
from agentdx.analysis.redundancy import RedundancyGroup
from agentdx.analysis.resilience import ResilienceResult
from agentdx.analysis.timing import ParallelismMetrics

_DOCS: Final = "docs/baseline-methodology.md"
_RULES_FILENAME: Final = "verdict_rules.toml"


# ---------------------------------------------------------------------------------------------
# Local enums — duplicated from `agentdx.scenario.schema`, deliberately not imported (I3; see
# `baseline.ComparabilityGrade`'s identical note and `timing.py`'s `_happens_before` precedent).
# ---------------------------------------------------------------------------------------------


class Severity(StrEnum):
    """Finding severity (PRD §18.3). Ordered `INFO < LOW < MEDIUM < HIGH < CRITICAL`."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: Final[dict[Severity, int]] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class VerdictClass(StrEnum):
    """PRD §18.1's nine verdict classes."""

    BENEFICIAL = "beneficial"
    NEUTRAL = "neutral"
    NEGATIVE_SPEEDUP = "negative_speedup"
    COORDINATION_BOTTLENECK = "coordination_bottleneck"
    STATE_CONFLICT_RISK = "state_conflict_risk"
    UNRELIABLE_TOPOLOGY = "unreliable_topology"
    NEGATIVE_CAPABILITY = "negative_capability"
    BASELINE_FAILED = "baseline_failed"
    BASELINE_CONTEXT_EXCEEDED = "baseline_context_exceeded"
    INSUFFICIENT_DATA = "insufficient_data"


#: PRD §18.1's literal precedence, highest first. The headline class is the first of these
#: whose trigger evaluates `True`; every other true trigger still contributes its findings
#: (`Verdict.secondary_classes`), never lost.
_PRECEDENCE: Final = (
    VerdictClass.UNRELIABLE_TOPOLOGY,
    VerdictClass.STATE_CONFLICT_RISK,
    VerdictClass.NEGATIVE_CAPABILITY,
    VerdictClass.NEGATIVE_SPEEDUP,
    VerdictClass.COORDINATION_BOTTLENECK,
    VerdictClass.BASELINE_FAILED,
    VerdictClass.BASELINE_CONTEXT_EXCEEDED,
    VerdictClass.NEUTRAL,
    VerdictClass.BENEFICIAL,
    VerdictClass.INSUFFICIENT_DATA,
)


class Confidence(StrEnum):
    """PRD §18.5's confidence levels — a function of data quality, never a fabricated interval."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------------------------
# The open seam for analysis.race (P12, NOT STARTED)
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class StateConflictFinding(Protocol):
    """The shape `verdict()` needs from a `state_conflict` finding.

    Matches `agentdx.scenario.assertions.Finding` exactly (`type`, `severity`,
    `evidence_seq`) — see the module docstring's "`analysis.race` does not exist yet" note.
    """

    type: str
    severity: str
    evidence_seq: Sequence[int]


# ---------------------------------------------------------------------------------------------
# Design Constraint 3 — evidence, enforced in the type (I6)
# ---------------------------------------------------------------------------------------------


class VerdictAnalysisError(RuntimeError):
    """A hard verdict-engine invariant failure — `E-VERD-0NN`."""

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


class EmptyEvidenceError(VerdictAnalysisError):
    """Raised by `Evidence.__post_init__` when `event_seqs` is empty.

    This **is** invariant I6 and PRD §18.4 ("a claim without evidence cannot be rendered"),
    enforced the moment the object is constructed — not by a schema validator running later,
    not by a reviewer reading a diff.
    """

    def __init__(self) -> None:
        """Build the fixed message for this one, specific, always-identical failure."""
        super().__init__(
            "E-VERD-001", "an Evidence with an empty event_seqs array is rejected (I6, PRD §18.4)"
        )


@dataclass(frozen=True, slots=True)
class Metric:
    """PRD §18.4's `metric` object — the named, valued, unit-carrying quantity behind a claim."""

    name: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class Evidence:
    """PRD §18.4's `evidence` object.

    Guarantees: `event_seqs` is non-empty (enforced in `__post_init__`, see `EmptyEvidenceError`)
    and sorted ascending; `computation` is always populated (never an empty string) — the
    deterministic formula name PRD §18.4 requires ("names the deterministic formula used").
    """

    event_seqs: tuple[int, ...]
    spans: tuple[str, ...]
    computation: str

    def __post_init__(self) -> None:
        """Reject construction outright when `event_seqs` is empty (I6, mechanised)."""
        if not self.event_seqs:
            raise EmptyEvidenceError()
        object.__setattr__(self, "event_seqs", tuple(sorted(self.event_seqs)))


@dataclass(frozen=True, slots=True)
class VerdictFinding:
    """One PRD §18.4-conformant finding — always evidence-backed by construction."""

    finding_id: str
    type: str
    severity: Severity
    claim: str
    metric: Metric
    evidence: Evidence
    confidence: str


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One PRD §18.6 recommendation — a deterministic-rule-table output, always evidence-backed."""

    trigger: str
    text: str
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class Verdict:
    """The full PRD §18 verdict.

    Guarantees: `verdict_class` is the highest-precedence trigger that evaluated `True`
    (`_PRECEDENCE`, PRD §18.1); `evidence` backs the headline claim itself and is never empty
    (same `Evidence` type, same enforcement); `coordination_score` is `None` only when no
    baseline comparison was available to compute a speedup component from — never a fabricated
    `0` (AGENTS.md §2).
    """

    verdict_class: VerdictClass
    secondary_classes: tuple[VerdictClass, ...]
    coordination_score: int | None
    confidence: Confidence
    findings: tuple[VerdictFinding, ...]
    recommendations: tuple[Recommendation, ...]
    evidence: Evidence


# ---------------------------------------------------------------------------------------------
# verdict_rules.toml
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerdictRules:
    """`verdict_rules.toml`'s `[verdict.*]` tables, typed and printable.

    `agentdx analyze --explain` (P17, not yet built) is what `format_rules` below serves.
    """

    beneficial_min_speedup: float
    neutral_min_speedup: float
    coordination_bottleneck_edge_cp_share: float
    coordination_bottleneck_agent_cp_share: float
    unreliable_topology_max_resilience: int
    insufficient_data_min_agents: int
    insufficient_data_min_spans: int
    insufficient_data_max_residual_fraction: float
    speedup_weight: float
    efficiency_weight: float
    reliability_weight: float
    conflict_penalty_per_finding: float
    conflict_penalty_max: float
    redundant_work_medium_fraction: float
    retry_amplification_medium: float
    orchestration_low_fraction: float
    redundancy_low_fraction: float
    high_max_residual_fraction: float
    medium_max_residual_fraction: float
    medium_max_instrumentation_gaps: int
    merge_edge_cp_share: float
    merge_dst_productive_work_max: float
    orchestration_recommend_fraction: float
    retry_amplification_recommend: float
    token_cost_multiplier_recommend: float
    token_cost_multiplier_speedup_ceiling: float
    raw: str
    """The verbatim `verdict_rules.toml` text — `format_rules(rules)` prints this, so
    `agentdx analyze --explain` is a byte-identical passthrough of the versioned file, per
    CONTEXT.md §3's "printable via `agentdx analyze --explain`"."""


_DEFAULTS: Final[dict[str, float | int]] = {
    "beneficial_min_speedup": 1.15,
    "neutral_min_speedup": 0.95,
    "coordination_bottleneck_edge_cp_share": 0.40,
    "coordination_bottleneck_agent_cp_share": 0.60,
    "unreliable_topology_max_resilience": 60,
    "insufficient_data_min_agents": 2,
    "insufficient_data_min_spans": 5,
    "insufficient_data_max_residual_fraction": 0.20,
    "speedup_weight": 40,
    "efficiency_weight": 25,
    "reliability_weight": 25,
    "conflict_penalty_per_finding": 10,
    "conflict_penalty_max": 25,
    "redundant_work_medium_fraction": 0.10,
    "retry_amplification_medium": 2.0,
    "orchestration_low_fraction": 0.15,
    "redundancy_low_fraction": 0.10,
    "high_max_residual_fraction": 0.02,
    "medium_max_residual_fraction": 0.05,
    "medium_max_instrumentation_gaps": 2,
    "merge_edge_cp_share": 0.40,
    "merge_dst_productive_work_max": 0.25,
    "orchestration_recommend_fraction": 0.15,
    "retry_amplification_recommend": 2.0,
    "token_cost_multiplier_recommend": 3.0,
    "token_cost_multiplier_speedup_ceiling": 1.2,
}


def _find_rules_path() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        toml_path = parent / _RULES_FILENAME
        if toml_path.is_file():
            return toml_path
        candidate = parent.parent / "src" / "agentdx" / "analysis" / _RULES_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_verdict_rules() -> VerdictRules:
    """Return `verdict_rules.toml`'s `[verdict.*]` tables, typed.

    Never a magic number inline (AGENTS.md §4, tripwire 5) — every threshold `verdict()`
    compares against comes from here.
    """
    path = _find_rules_path()
    if path is None:
        raw = ""
        data: dict[str, object] = {}
    else:
        raw = path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)

    verdict_section = data.get("verdict", {})
    classes = verdict_section.get("classes", {}) if isinstance(verdict_section, dict) else {}
    score_section = verdict_section.get("score", {}) if isinstance(verdict_section, dict) else {}
    severity = verdict_section.get("severity", {}) if isinstance(verdict_section, dict) else {}
    confidence = verdict_section.get("confidence", {}) if isinstance(verdict_section, dict) else {}
    recs = verdict_section.get("recommendations", {}) if isinstance(verdict_section, dict) else {}
    merged: dict[str, float | int] = dict(_DEFAULTS)
    for section in (classes, score_section, severity, confidence, recs):
        if isinstance(section, dict):
            for key, value in section.items():
                if key in merged and isinstance(value, int | float) and not isinstance(value, bool):
                    merged[key] = value

    return VerdictRules(
        beneficial_min_speedup=float(merged["beneficial_min_speedup"]),
        neutral_min_speedup=float(merged["neutral_min_speedup"]),
        coordination_bottleneck_edge_cp_share=float(
            merged["coordination_bottleneck_edge_cp_share"]
        ),
        coordination_bottleneck_agent_cp_share=float(
            merged["coordination_bottleneck_agent_cp_share"]
        ),
        unreliable_topology_max_resilience=int(merged["unreliable_topology_max_resilience"]),
        insufficient_data_min_agents=int(merged["insufficient_data_min_agents"]),
        insufficient_data_min_spans=int(merged["insufficient_data_min_spans"]),
        insufficient_data_max_residual_fraction=float(
            merged["insufficient_data_max_residual_fraction"]
        ),
        speedup_weight=float(merged["speedup_weight"]),
        efficiency_weight=float(merged["efficiency_weight"]),
        reliability_weight=float(merged["reliability_weight"]),
        conflict_penalty_per_finding=float(merged["conflict_penalty_per_finding"]),
        conflict_penalty_max=float(merged["conflict_penalty_max"]),
        redundant_work_medium_fraction=float(merged["redundant_work_medium_fraction"]),
        retry_amplification_medium=float(merged["retry_amplification_medium"]),
        orchestration_low_fraction=float(merged["orchestration_low_fraction"]),
        redundancy_low_fraction=float(merged["redundancy_low_fraction"]),
        high_max_residual_fraction=float(merged["high_max_residual_fraction"]),
        medium_max_residual_fraction=float(merged["medium_max_residual_fraction"]),
        medium_max_instrumentation_gaps=int(merged["medium_max_instrumentation_gaps"]),
        merge_edge_cp_share=float(merged["merge_edge_cp_share"]),
        merge_dst_productive_work_max=float(merged["merge_dst_productive_work_max"]),
        orchestration_recommend_fraction=float(merged["orchestration_recommend_fraction"]),
        retry_amplification_recommend=float(merged["retry_amplification_recommend"]),
        token_cost_multiplier_recommend=float(merged["token_cost_multiplier_recommend"]),
        token_cost_multiplier_speedup_ceiling=float(
            merged["token_cost_multiplier_speedup_ceiling"]
        ),
        raw=raw,
    )


def format_rules(rules: VerdictRules) -> str:
    """Return the verbatim `verdict_rules.toml` text `rules` was loaded from.

    What `agentdx analyze --explain` (P17, not yet built) prints — CONTEXT.md §3's "printable"
    requirement, satisfied today by this function even though no CLI command calls it yet.
    """
    return rules.raw


# ---------------------------------------------------------------------------------------------
# PRD §18.3 — severity, and the mechanical findings this module can produce today
# ---------------------------------------------------------------------------------------------


def _count_high_or_critical(findings: Sequence[StateConflictFinding]) -> int:
    high_or_critical = (Severity.HIGH.value, Severity.CRITICAL.value)
    return sum(1 for f in findings if f.type == "state_conflict" and f.severity in high_or_critical)


def _coordination_bottleneck_findings(
    edge_aggregates: Sequence[EdgeAggregate],
    agent_aggregates: Sequence[AgentAggregate],
    virtual_makespan_ms: int,
    rules: VerdictRules,
) -> tuple[VerdictFinding, ...]:
    findings: list[VerdictFinding] = []
    for edge in edge_aggregates:
        if edge.cp_share >= rules.coordination_bottleneck_edge_cp_share:
            findings.append(
                VerdictFinding(
                    finding_id=f"bottleneck-edge-{edge.src_agent_id}-{edge.dst_agent_id}",
                    type="coordination_bottleneck",
                    severity=Severity.HIGH,
                    claim=(
                        f"handoff on {edge.src_agent_id}->{edge.dst_agent_id} accounts for "
                        f"{edge.cp_share:.0%} of critical-path time"
                    ),
                    metric=Metric(name="edge.cp_share", value=edge.cp_share, unit="ratio"),
                    evidence=Evidence(
                        event_seqs=edge.evidence_seq,
                        spans=(),
                        computation="sum(recv.virtual_ts - send.virtual_ts for CP edges on "
                        f"{edge.src_agent_id}->{edge.dst_agent_id}) / makespan",
                    ),
                    confidence=Confidence.HIGH.value,
                )
            )
    if virtual_makespan_ms > 0:
        for agent in agent_aggregates:
            share = agent.cp_ms / virtual_makespan_ms
            if share >= rules.coordination_bottleneck_agent_cp_share:
                findings.append(
                    VerdictFinding(
                        finding_id=f"bottleneck-agent-{agent.agent_id}",
                        type="coordination_bottleneck",
                        severity=Severity.HIGH,
                        claim=f"{agent.agent_id} occupies {share:.0%} of the critical path",
                        metric=Metric(name="agent.cp_share", value=share, unit="ratio"),
                        evidence=Evidence(
                            event_seqs=agent.evidence_seq,
                            spans=(),
                            computation="agent.cp_ms / virtual_makespan_ms",
                        ),
                        confidence=Confidence.HIGH.value,
                    )
                )
    return tuple(findings)


def _redundancy_findings(
    redundancy_groups: Sequence[RedundancyGroup],
    total_work: TotalWorkDecomposition | None,
    rules: VerdictRules,
) -> tuple[VerdictFinding, ...]:
    findings: list[VerdictFinding] = []
    for group in redundancy_groups:
        fraction = (
            group.wasted_virtual_ms / total_work.total_work_ms
            if total_work is not None and total_work.total_work_ms > 0
            else 0.0
        )
        severity = (
            Severity.MEDIUM if fraction > rules.redundant_work_medium_fraction else Severity.LOW
        )
        findings.append(
            VerdictFinding(
                finding_id=f"redundancy-{group.group_key}",
                type="redundancy",
                severity=severity,
                claim=(
                    f"{group.tool_name} executed {len(group.member_node_ids)}x concurrently; "
                    f"{group.wasted_virtual_ms}ms and {group.wasted_tokens} tokens wasted"
                ),
                metric=Metric(
                    name="redundancy.wasted_fraction_of_total_work", value=fraction, unit="ratio"
                ),
                evidence=Evidence(
                    event_seqs=group.evidence_seq,
                    spans=tuple(group.member_node_ids),
                    computation="sum(durations) - max(duration) over the redundancy group",
                ),
                confidence=Confidence.HIGH.value,
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------------------------
# PRD §18.6 — recommendations
# ---------------------------------------------------------------------------------------------


def _merge_recommendations(
    edge_aggregates: Sequence[EdgeAggregate],
    agent_aggregates: Sequence[AgentAggregate],
    total_work: TotalWorkDecomposition | None,
    rules: VerdictRules,
) -> tuple[Recommendation, ...]:
    productive_by_agent: dict[str, float] = {}
    if total_work is not None and total_work.total_work_ms > 0:
        # `TotalWorkDecomposition` has no per-agent split; a per-agent productive-work share
        # needs `agent_aggregates` (cp_ms is the closest already-available proxy — see the
        # docstring note below) rather than re-deriving it from raw events (I3/no re-derivation).
        pass
    agents_by_id = {a.agent_id: a for a in agent_aggregates}
    recs: list[Recommendation] = []
    for edge in edge_aggregates:
        if edge.cp_share < rules.merge_edge_cp_share:
            continue
        dst = agents_by_id.get(edge.dst_agent_id)
        # PRD's trigger needs "B's productive work < 25% of the run" — approximated with the
        # destination agent's own cp_ms share of the makespan (the closest field this module's
        # already-computed aggregates carry without re-deriving a new metric outside DELIVERABLES
        # — declared, not silently assumed identical to "productive work share").
        dst_share = productive_by_agent.get(edge.dst_agent_id)
        if dst_share is None and dst is not None and dst.busy_ms + dst.idle_ms > 0:
            dst_share = dst.busy_ms / (dst.busy_ms + dst.idle_ms)
        if dst_share is not None and dst_share >= rules.merge_dst_productive_work_max:
            continue
        recs.append(
            Recommendation(
                trigger="edge.cp_share >= merge_edge_cp_share",
                text=(
                    f"Merge `{edge.dst_agent_id}` into `{edge.src_agent_id}`; the handoff on "
                    f"that edge accounts for {edge.cp_share:.0%} of critical-path time."
                ),
                evidence=Evidence(
                    event_seqs=edge.evidence_seq,
                    spans=(),
                    computation="edge.cp_handoff_ms / virtual_makespan_ms",
                ),
            )
        )
    return tuple(recs)


def _orchestration_recommendation(
    decomposition: OverheadDecomposition | None, rules: VerdictRules
) -> Recommendation | None:
    if decomposition is None or decomposition.virtual_makespan_ms <= 0:
        return None
    fraction = decomposition.bucket_ms["orchestration"] / decomposition.virtual_makespan_ms
    if fraction <= rules.orchestration_recommend_fraction:
        return None
    evidence_seq = decomposition.bucket_evidence_seq["orchestration"]
    if not evidence_seq:
        return None
    return Recommendation(
        trigger="orchestration > orchestration_recommend_fraction of CP",
        text=(
            f"Supervisor deliberation is {fraction:.0%} of critical-path time; consider static "
            "routing for the deterministic branches."
        ),
        evidence=Evidence(
            event_seqs=evidence_seq,
            spans=(),
            computation="bucket_ms['orchestration'] / virtual_makespan_ms",
        ),
    )


def _redundancy_recommendations(
    redundancy_groups: Sequence[RedundancyGroup],
) -> tuple[Recommendation, ...]:
    recs: list[Recommendation] = []
    for group in redundancy_groups:
        recs.append(
            Recommendation(
                trigger="redundancy group detected",
                text=(
                    f"`{group.tool_name}` executed {len(group.member_node_ids)}x concurrently; "
                    f"{group.wasted_virtual_ms}ms and {group.wasted_tokens} tokens wasted. "
                    "Memoise the tool or assign it to one agent."
                ),
                evidence=Evidence(
                    event_seqs=group.evidence_seq,
                    spans=tuple(group.member_node_ids),
                    computation="sum(durations) - max(duration) over the redundancy group",
                ),
            )
        )
    return tuple(recs)


def _token_cost_recommendation(
    comparison: BaselineComparison | None, rules: VerdictRules
) -> Recommendation | None:
    if comparison is None:
        return None
    if comparison.token_cost_multiplier <= rules.token_cost_multiplier_recommend:
        return None
    if comparison.achieved_speedup >= rules.token_cost_multiplier_speedup_ceiling:
        return None
    return Recommendation(
        trigger="token_cost_multiplier > threshold with achieved_speedup below ceiling",
        text=(
            f"The topology costs {comparison.token_cost_multiplier:.1f}x the tokens for "
            f"{comparison.achieved_speedup:.2f}x the speed; a single agent is the better trade "
            "at this task size."
        ),
        evidence=Evidence(
            event_seqs=comparison.evidence_seq,
            spans=(),
            computation="tokens_multi / tokens_baseline",
        ),
    )


# ---------------------------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------------------------


def _class_triggers(
    *,
    comparison: BaselineComparison | None,
    decomposition: OverheadDecomposition | None,
    resilience: ResilienceResult | None,
    edge_aggregates: Sequence[EdgeAggregate],
    agent_aggregates: Sequence[AgentAggregate],
    state_conflict_findings: Sequence[StateConflictFinding],
    agent_count: int,
    span_count: int,
    virtual_makespan_ms: int,
    rules: VerdictRules,
) -> dict[VerdictClass, bool]:
    high_or_critical_conflicts = _count_high_or_critical(state_conflict_findings)

    unreliable = resilience is not None and (
        resilience.silent_failure_capped
        or (
            resilience.resilience_score is not None
            and resilience.resilience_score < rules.unreliable_topology_max_resilience
        )
    )
    state_conflict_risk = high_or_critical_conflicts > 0
    negative_capability = (
        comparison is not None
        and comparison.outcome_multi != "complete"
        and comparison.outcome_baseline is BaselineOutcome.COMPLETED
    )
    negative_speedup = (
        comparison is not None
        and comparison.outcome_multi == "complete"
        and comparison.outcome_baseline is BaselineOutcome.COMPLETED
        and comparison.achieved_speedup < rules.neutral_min_speedup
    )
    coordination_bottleneck = any(
        e.cp_share >= rules.coordination_bottleneck_edge_cp_share for e in edge_aggregates
    ) or (
        virtual_makespan_ms > 0
        and any(
            a.cp_ms / virtual_makespan_ms >= rules.coordination_bottleneck_agent_cp_share
            for a in agent_aggregates
        )
    )
    baseline_failed = (
        comparison is not None and comparison.outcome_baseline is BaselineOutcome.FAILED
    )
    baseline_context_exceeded = (
        comparison is not None and comparison.outcome_baseline is BaselineOutcome.CONTEXT_EXCEEDED
    )
    neutral = (
        comparison is not None
        and comparison.outcome_multi == "complete"
        and comparison.outcome_baseline is BaselineOutcome.COMPLETED
        and rules.neutral_min_speedup <= comparison.achieved_speedup < rules.beneficial_min_speedup
        and high_or_critical_conflicts == 0
    )
    beneficial = (
        comparison is not None
        and comparison.outcome_multi == "complete"
        and comparison.outcome_baseline is BaselineOutcome.COMPLETED
        and comparison.achieved_speedup >= rules.beneficial_min_speedup
        and high_or_critical_conflicts == 0
    )
    residual_fraction = decomposition.residual_fraction if decomposition is not None else 0.0
    insufficient_data = (
        agent_count < rules.insufficient_data_min_agents
        or span_count < rules.insufficient_data_min_spans
        or residual_fraction > rules.insufficient_data_max_residual_fraction
        or comparison is None
    )

    return {
        VerdictClass.UNRELIABLE_TOPOLOGY: unreliable,
        VerdictClass.STATE_CONFLICT_RISK: state_conflict_risk,
        VerdictClass.NEGATIVE_CAPABILITY: negative_capability,
        VerdictClass.NEGATIVE_SPEEDUP: negative_speedup,
        VerdictClass.COORDINATION_BOTTLENECK: coordination_bottleneck,
        VerdictClass.BASELINE_FAILED: baseline_failed,
        VerdictClass.BASELINE_CONTEXT_EXCEEDED: baseline_context_exceeded,
        VerdictClass.NEUTRAL: neutral,
        VerdictClass.BENEFICIAL: beneficial,
        VerdictClass.INSUFFICIENT_DATA: insufficient_data,
    }


def _coordination_score(
    *,
    comparison: BaselineComparison | None,
    decomposition: OverheadDecomposition | None,
    resilience: ResilienceResult | None,
    state_conflict_findings: Sequence[StateConflictFinding],
    rules: VerdictRules,
) -> int | None:
    if comparison is None:
        return None
    ideal = max(1.0, comparison.ideal_parallel_speedup)
    speedup_ratio = max(0.0, min(1.0, comparison.achieved_speedup / ideal))
    speedup_component = speedup_ratio * rules.speedup_weight

    if decomposition is not None and decomposition.virtual_makespan_ms > 0:
        productive_share = (
            decomposition.bucket_ms["productive_work"] / decomposition.virtual_makespan_ms
        )
    else:
        productive_share = 0.0
    efficiency_component = max(0.0, min(1.0, productive_share)) * rules.efficiency_weight

    if resilience is not None and resilience.resilience_score is not None:
        reliability_component = (resilience.resilience_score / 100) * rules.reliability_weight
    else:
        reliability_component = rules.reliability_weight  # "25 if no chaos run" — PRD §18.2

    conflict_penalty = min(
        rules.conflict_penalty_max,
        rules.conflict_penalty_per_finding * _count_high_or_critical(state_conflict_findings),
    )

    total = speedup_component + efficiency_component + reliability_component - conflict_penalty
    return round(total)


def _confidence(
    *,
    comparison: BaselineComparison | None,
    decomposition: OverheadDecomposition | None,
    instrumentation_gap_count: int,
    rules: VerdictRules,
) -> Confidence:
    residual_fraction = decomposition.residual_fraction if decomposition is not None else 1.0
    grade = comparison.comparability.grade if comparison is not None else None

    low = (
        residual_fraction > rules.medium_max_residual_fraction
        or grade == ComparabilityGrade.C
        or comparison is None
    )
    if low:
        return Confidence.LOW
    medium = (
        residual_fraction > rules.high_max_residual_fraction
        or grade == ComparabilityGrade.B
        or instrumentation_gap_count > 0
    )
    if medium:
        return Confidence.MEDIUM
    return Confidence.HIGH


def verdict(
    *,
    comparison: BaselineComparison | None,
    decomposition: OverheadDecomposition | None = None,
    total_work: TotalWorkDecomposition | None = None,
    resilience: ResilienceResult | None = None,
    edge_aggregates: Sequence[EdgeAggregate] = (),
    agent_aggregates: Sequence[AgentAggregate] = (),
    redundancy_groups: Sequence[RedundancyGroup] = (),
    state_conflict_findings: Sequence[StateConflictFinding] = (),
    parallelism: ParallelismMetrics | None = None,
    agent_count: int,
    span_count: int,
    instrumentation_gap_count: int = 0,
    run_start_seq: int | None = None,
    rules: VerdictRules | None = None,
) -> Verdict:
    """Compute the PRD §18 verdict from already-computed analysis outputs.

    Args:
        comparison: `baseline.compare()`'s result, or `None` if no baseline was generated
            (`INSUFFICIENT_DATA` follows, per this module's documented extension of §18.1's
            row — see the module docstring).
        decomposition: `overhead.decompose_critical_path()`'s result, for `efficiency_component`
            and `confidence`'s residual check.
        total_work: `overhead.decompose_total_work()`'s result (accepted for symmetry with
            `baseline.compare`'s own signature; not currently read by any rule below —
            `redundancy` findings use `RedundancyGroup.wasted_virtual_ms`/`wasted_tokens`
            directly, which do not need it).
        resilience: `resilience.score()`'s result, or `None` if no chaos run was executed
            (PRD §18.2: "25 if no chaos run").
        edge_aggregates: `aggregates.compute_edge_aggregates()`'s result, for
            `COORDINATION_BOTTLENECK` and the merge recommendation.
        agent_aggregates: `aggregates.compute_agent_aggregates()`'s result, for
            `COORDINATION_BOTTLENECK`.
        redundancy_groups: `redundancy.detect_redundancy()`'s result.
        state_conflict_findings: See the module docstring's "`analysis.race` does not exist
            yet" note. Defaults to empty.
        parallelism: `timing.parallelism_metrics()`'s result (accepted, not currently read —
            `ideal_parallel_speedup` for the score comes from `comparison` directly, which
            already carries it).
        agent_count: For `INSUFFICIENT_DATA`'s `<2 agents` trigger.
        span_count: For `INSUFFICIENT_DATA`'s `<5 spans` trigger.
        instrumentation_gap_count: Count of `instrumentation_gap` events in the run, for
            `confidence`'s "≤2 instrumentation gaps" condition.
        run_start_seq: The run's own `run_start.seq`, if the caller has it, for I6 evidence on
            the `INSUFFICIENT_DATA`-with-no-comparison-and-no-findings fallback below — this
            function otherwise has no raw event log to point evidence at. An OP-2 audit
            (2026-08-18) found the fallback previously hardcoded a placeholder `event_seqs=(0,)`
            unconditionally, assumed rather than verified against the actual run; a real caller
            (P17's future CLI) that has the run's own event log should supply the real value
            here. Falls back to the documented placeholder when omitted (`None`), which remains
            a soft I6 gap when no caller can supply it — see `docs/baseline-methodology.md` §5.
        rules: Override for `load_verdict_rules()`'s defaults (mainly for tests).

    Returns:
        The `Verdict`. `verdict_class` is always one of PRD §18.1's nine classes — at least one
        trigger always evaluates `True` (`INSUFFICIENT_DATA`'s trigger includes `comparison is
        None`, so it is the guaranteed fallback).
    """
    active_rules = rules if rules is not None else load_verdict_rules()
    virtual_makespan_ms = comparison.virtual_makespan_multi_ms if comparison is not None else 0

    triggers = _class_triggers(
        comparison=comparison,
        decomposition=decomposition,
        resilience=resilience,
        edge_aggregates=edge_aggregates,
        agent_aggregates=agent_aggregates,
        state_conflict_findings=state_conflict_findings,
        agent_count=agent_count,
        span_count=span_count,
        virtual_makespan_ms=virtual_makespan_ms,
        rules=active_rules,
    )
    headline = next((c for c in _PRECEDENCE if triggers[c]), VerdictClass.INSUFFICIENT_DATA)
    secondary = tuple(c for c in _PRECEDENCE if c != headline and triggers[c])

    findings: list[VerdictFinding] = []
    findings.extend(
        _coordination_bottleneck_findings(
            edge_aggregates, agent_aggregates, virtual_makespan_ms, active_rules
        )
    )
    findings.extend(_redundancy_findings(redundancy_groups, total_work, active_rules))

    recommendations: list[Recommendation] = []
    recommendations.extend(
        _merge_recommendations(edge_aggregates, agent_aggregates, total_work, active_rules)
    )
    orchestration_rec = _orchestration_recommendation(decomposition, active_rules)
    if orchestration_rec is not None:
        recommendations.append(orchestration_rec)
    recommendations.extend(_redundancy_recommendations(redundancy_groups))
    token_rec = _token_cost_recommendation(comparison, active_rules)
    if token_rec is not None:
        recommendations.append(token_rec)

    score = _coordination_score(
        comparison=comparison,
        decomposition=decomposition,
        resilience=resilience,
        state_conflict_findings=state_conflict_findings,
        rules=active_rules,
    )
    confidence = _confidence(
        comparison=comparison,
        decomposition=decomposition,
        instrumentation_gap_count=instrumentation_gap_count,
        rules=active_rules,
    )

    if comparison is not None:
        top_evidence = Evidence(
            event_seqs=comparison.evidence_seq,
            spans=(),
            computation=f"verdict_class={headline.value}",
        )
    elif findings:
        top_evidence = findings[0].evidence
    elif run_start_seq is not None:
        top_evidence = Evidence(
            event_seqs=(run_start_seq,),
            spans=(),
            computation=(
                f"no baseline comparison and no mechanically-detected finding — "
                f"INSUFFICIENT_DATA points at the caller-supplied run_start_seq={run_start_seq}"
            ),
        )
    else:
        top_evidence = Evidence(
            event_seqs=(0,),
            spans=(),
            computation=(
                "no baseline comparison, no mechanically-detected finding, and no run_start_seq "
                "supplied — INSUFFICIENT_DATA carries an assumed placeholder seq 0 rather than "
                "an empty array (I6), UNVERIFIED against the actual run's log: this function "
                "has no raw events to check seq 0 exists. A caller with the run's own event log "
                "should pass run_start_seq= for real evidence (found by an OP-2 audit, "
                "2026-08-18 — see docs/baseline-methodology.md §5)"
            ),
        )

    return Verdict(
        verdict_class=headline,
        secondary_classes=secondary,
        coordination_score=score,
        confidence=confidence,
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        evidence=top_evidence,
    )


__all__ = [
    "SEVERITY_ORDER",
    "Confidence",
    "EmptyEvidenceError",
    "Evidence",
    "Metric",
    "Recommendation",
    "Severity",
    "StateConflictFinding",
    "Verdict",
    "VerdictAnalysisError",
    "VerdictClass",
    "VerdictFinding",
    "VerdictRules",
    "format_rules",
    "load_verdict_rules",
    "verdict",
]
