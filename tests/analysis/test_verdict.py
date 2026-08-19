"""PRD §18 — the verdict engine: class precedence, evidence, score, confidence, recommendations.

**Why almost nothing here builds an event log.** `verdict()`'s own module docstring is explicit:
it is "a pure function of analysis outputs" — `BaselineComparison`, `OverheadDecomposition`,
`ResilienceResult`, `EdgeAggregate`/`AgentAggregate`, `RedundancyGroup` — never raw events, and
every one of those is a plain frozen dataclass with fields a test can set directly. Exercising
`verdict()`'s own logic (precedence, score, confidence, evidence bundling) through hand-built
inputs is more direct, and less coupled to `timing`/`overhead`/`redundancy`/`aggregates`'
already-independently-tested internals, than deriving the same inputs from a real event log
would be. Small helper factories below (`_comparison`, `_decomposition`, `_edge`, `_agent`,
`_resilience`, `_conflict_finding`) build minimally-valid instances of each, overridable via
`dataclasses.replace`-style keyword defaults.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import pytest

from agentdx.analysis.aggregates import AgentAggregate, EdgeAggregate
from agentdx.analysis.baseline import (
    BaselineComparison,
    BaselineOutcome,
    ComparabilityAssessment,
    ComparabilityGrade,
)
from agentdx.analysis.overhead import OverheadDecomposition
from agentdx.analysis.redundancy import RedundancyGroup
from agentdx.analysis.resilience import ResilienceResult
from agentdx.analysis.verdict import (
    Confidence,
    EmptyEvidenceError,
    Evidence,
    VerdictClass,
    load_verdict_rules,
    verdict,
)

# ---------------------------------------------------------------------------------------------
# Minimal-but-valid factories for every analysis output `verdict()` consumes
# ---------------------------------------------------------------------------------------------


def _comparability(grade: ComparabilityGrade = ComparabilityGrade.A) -> ComparabilityAssessment:
    return ComparabilityAssessment(
        grade=grade,
        cache_reuse_rate=0.9,
        cache_reuse_tool_rate=0.9,
        cache_reuse_llm_rate=0.9,
        model_match=True,
        tools_match=True,
        both_succeeded=True,
        reason="cache reuse 90%, identical model/tools/task, both runs succeeded",
    )


def _comparison(
    *,
    achieved_speedup: float = 1.3,
    ideal_parallel_speedup: float = 1.2,
    outcome_multi: str = "complete",
    outcome_baseline: BaselineOutcome = BaselineOutcome.COMPLETED,
    grade: ComparabilityGrade = ComparabilityGrade.A,
    virtual_makespan_multi_ms: int = 100,
) -> BaselineComparison:
    return BaselineComparison(
        multi_run_id="r_test01",
        baseline_of="r_test01",
        achieved_speedup=achieved_speedup,
        ideal_parallel_speedup=ideal_parallel_speedup,
        overhead_cost=achieved_speedup - ideal_parallel_speedup,
        token_cost_multiplier=1.0,
        cost_efficiency=achieved_speedup,
        gap=ideal_parallel_speedup - achieved_speedup,
        attribution=(),
        comparability=_comparability(grade),
        virtual_makespan_multi_ms=virtual_makespan_multi_ms,
        virtual_makespan_baseline_ms=round(virtual_makespan_multi_ms * achieved_speedup),
        total_work_ms=virtual_makespan_multi_ms,
        critical_path_ms=virtual_makespan_multi_ms,
        tokens_multi=100,
        tokens_baseline=100,
        outcome_multi=outcome_multi,
        outcome_baseline=outcome_baseline,
        evidence_seq=(0, 1),
    )


def _decomposition(
    *, residual_fraction: float = 0.01, productive_ms: int = 80, virtual_makespan_ms: int = 100
) -> OverheadDecomposition:
    non_productive = virtual_makespan_ms - productive_ms
    bucket_ms = {
        "retry_recovery": 0,
        "redundant_work": 0,
        "orchestration": 0,
        "productive_work": productive_ms,
        "handoff": non_productive,
        "blocking_wait": 0,
    }
    bucket_evidence_seq = {name: ((1,) if ms > 0 else ()) for name, ms in bucket_ms.items()}
    residual_ms = round(residual_fraction * virtual_makespan_ms)
    return OverheadDecomposition(
        bucket_ms=bucket_ms,
        bucket_evidence_seq=bucket_evidence_seq,
        residual_ms=residual_ms,
        residual_fraction=residual_fraction,
        residual_flagged=residual_fraction >= 0.02,
        residual_tolerance=0.02,
        virtual_makespan_ms=virtual_makespan_ms,
        critical_path_length_ms=virtual_makespan_ms,
    )


def _edge(
    *, src: str = "a", dst: str = "b", cp_share: float = 0.1, evidence_seq: tuple[int, ...] = (1, 2)
) -> EdgeAggregate:
    return EdgeAggregate(
        src_agent_id=src,
        dst_agent_id=dst,
        message_count=1,
        total_handoff_ms=10,
        cp_handoff_ms=10,
        cp_share=cp_share,
        evidence_seq=evidence_seq,
    )


def _agent(
    *, agent_id: str = "a", cp_ms: int = 10, evidence_seq: tuple[int, ...] = (1, 2)
) -> AgentAggregate:
    return AgentAggregate(
        agent_id=agent_id,
        busy_ms=cp_ms,
        idle_ms=0,
        cp_ms=cp_ms,
        tokens=0,
        evidence_seq=evidence_seq,
    )


def _redundancy_group(*, wasted_ms: int = 5) -> RedundancyGroup:
    return RedundancyGroup(
        group_key="fetch:h1",
        tool_name="fetch",
        args_hash="h1",
        member_node_ids=("n1", "n2"),
        representative_node_id="n2",
        wasted_virtual_ms=wasted_ms,
        wasted_tokens=0,
        evidence_seq=(1, 2),
    )


def _resilience(*, score: int | None = 80, silent_failure_capped: bool = False) -> ResilienceResult:
    return ResilienceResult(
        resilience_score=score,
        worst_fault_score=score,
        n_faults=1,
        per_fault=(),
        not_fired=(),
        aborted=(),
        silent_failure_capped=silent_failure_capped,
        evidence_seq=(1,),
    )


@dataclass(slots=True)
class _ConflictFinding:
    """A hand-built `StateConflictFinding` — `analysis.race` (P12) does not exist yet.

    Deliberately **not** `frozen=True`, unlike every other dataclass in this file: `Protocol`
    data attributes are implicitly settable (mypy: "expected settable variable, got read-only
    attribute"), so a frozen dataclass fails `StateConflictFinding`'s structural check even
    though nothing here ever mutates an instance after construction. `evidence_seq` is typed
    `Sequence[int]`, matching the Protocol's own annotation exactly.
    """

    type: str
    severity: str
    evidence_seq: Sequence[int]


def _conflict_finding(severity: str = "high") -> _ConflictFinding:
    return _ConflictFinding(type="state_conflict", severity=severity, evidence_seq=(3, 4))


# ---------------------------------------------------------------------------------------------
# Design Constraint 3 / I6 — the schema test the Definition of Done names explicitly
# ---------------------------------------------------------------------------------------------


def test_an_empty_evidence_array_is_rejected_by_the_type_itself() -> None:
    """The Definition of Done's schema test: an empty `event_seqs` cannot construct `Evidence`."""
    with pytest.raises(EmptyEvidenceError) as excinfo:
        Evidence(event_seqs=(), spans=(), computation="anything")
    assert excinfo.value.code == "E-VERD-001"


def test_evidence_sorts_its_event_seqs() -> None:
    ev = Evidence(event_seqs=(5, 1, 3), spans=(), computation="x")
    assert ev.event_seqs == (1, 3, 5)


def test_every_verdict_finding_and_recommendation_carries_nonempty_evidence() -> None:
    """A structural sweep, not just the one constructor test.

    Nothing `verdict()` returns can have an empty `Evidence` (the type would have raised
    during construction).
    """
    result = verdict(
        comparison=_comparison(),
        decomposition=_decomposition(),
        edge_aggregates=(_edge(cp_share=0.5),),
        agent_aggregates=(_agent(),),
        redundancy_groups=(_redundancy_group(),),
        agent_count=2,
        span_count=10,
    )
    assert result.evidence.event_seqs
    for finding in result.findings:
        assert finding.evidence.event_seqs
    for rec in result.recommendations:
        assert rec.evidence.event_seqs


# ---------------------------------------------------------------------------------------------
# STATE_CONFLICT_RISK — never fires without findings (the module docstring's open-seam note)
# ---------------------------------------------------------------------------------------------


def test_state_conflict_risk_never_fires_without_findings() -> None:
    """`analysis.race` (P12) doesn't exist.

    The default empty `state_conflict_findings` must never trip this class, in either the
    headline or secondary slot.
    """
    result = verdict(comparison=_comparison(achieved_speedup=1.5), agent_count=2, span_count=10)
    assert result.verdict_class is not VerdictClass.STATE_CONFLICT_RISK
    assert VerdictClass.STATE_CONFLICT_RISK not in result.secondary_classes


def test_state_conflict_risk_fires_on_a_high_severity_finding() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        state_conflict_findings=(_conflict_finding("high"),),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.STATE_CONFLICT_RISK


def test_state_conflict_risk_ignores_low_and_medium_severity() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        state_conflict_findings=(_conflict_finding("low"), _conflict_finding("medium")),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is not VerdictClass.STATE_CONFLICT_RISK


# ---------------------------------------------------------------------------------------------
# Every verdict class, triggered directly, plus the precedence order
# ---------------------------------------------------------------------------------------------


def test_beneficial() -> None:
    result = verdict(comparison=_comparison(achieved_speedup=1.5), agent_count=2, span_count=10)
    assert result.verdict_class is VerdictClass.BENEFICIAL


def test_neutral() -> None:
    result = verdict(comparison=_comparison(achieved_speedup=1.0), agent_count=2, span_count=10)
    assert result.verdict_class is VerdictClass.NEUTRAL


def test_negative_speedup() -> None:
    result = verdict(comparison=_comparison(achieved_speedup=0.5), agent_count=2, span_count=10)
    assert result.verdict_class is VerdictClass.NEGATIVE_SPEEDUP


def test_coordination_bottleneck_via_edge_cp_share() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        edge_aggregates=(_edge(cp_share=0.6),),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.COORDINATION_BOTTLENECK
    assert any(f.type == "coordination_bottleneck" for f in result.findings)


def test_coordination_bottleneck_via_agent_cp_share() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5, virtual_makespan_multi_ms=100),
        agent_aggregates=(_agent(cp_ms=70),),  # 70/100 = 0.70 >= 0.60 default threshold
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.COORDINATION_BOTTLENECK


def test_negative_capability() -> None:
    result = verdict(
        comparison=_comparison(outcome_multi="failed", outcome_baseline=BaselineOutcome.COMPLETED),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.NEGATIVE_CAPABILITY


def test_baseline_failed() -> None:
    result = verdict(
        comparison=_comparison(outcome_baseline=BaselineOutcome.FAILED),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.BASELINE_FAILED


def test_baseline_context_exceeded() -> None:
    result = verdict(
        comparison=_comparison(outcome_baseline=BaselineOutcome.CONTEXT_EXCEEDED),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.BASELINE_CONTEXT_EXCEEDED


def test_unreliable_topology_via_low_resilience_score() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        resilience=_resilience(score=40),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.UNRELIABLE_TOPOLOGY


def test_unreliable_topology_via_silent_failure_cap_regardless_of_score() -> None:
    """Even a resilience score that would otherwise pass the threshold — capped means unreliable."""
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        resilience=_resilience(score=90, silent_failure_capped=True),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.UNRELIABLE_TOPOLOGY


def test_insufficient_data_via_too_few_agents() -> None:
    """`INSUFFICIENT_DATA` is `_PRECEDENCE`'s lowest-priority trigger.

    The module docstring's guaranteed fallback — with a strong `BENEFICIAL` signal also true,
    `BENEFICIAL` still wins the headline, but `INSUFFICIENT_DATA` is not lost: it is reported
    as a secondary class, the honest caveat that this speedup number rests on very little data.
    """
    result = verdict(comparison=_comparison(achieved_speedup=1.5), agent_count=1, span_count=10)
    assert result.verdict_class is VerdictClass.BENEFICIAL
    assert VerdictClass.INSUFFICIENT_DATA in result.secondary_classes


def test_insufficient_data_via_too_few_spans() -> None:
    result = verdict(comparison=_comparison(achieved_speedup=1.5), agent_count=2, span_count=2)
    assert VerdictClass.INSUFFICIENT_DATA in result.secondary_classes


def test_insufficient_data_via_high_residual_fraction() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        decomposition=_decomposition(residual_fraction=0.5),
        agent_count=2,
        span_count=10,
    )
    assert VerdictClass.INSUFFICIENT_DATA in result.secondary_classes


def test_insufficient_data_is_the_guaranteed_fallback_with_no_comparison_at_all() -> None:
    result = verdict(comparison=None, agent_count=2, span_count=10)
    assert result.verdict_class is VerdictClass.INSUFFICIENT_DATA
    assert result.evidence.event_seqs  # I6 — even the fallback carries evidence, never empty
    # Without run_start_seq, the fallback is an honestly-labelled, unverified placeholder — not
    # a traced seq (an OP-2 audit, 2026-08-18, found the untagged version of this claimed more
    # than it could verify; see verdict()'s run_start_seq docstring).
    assert result.evidence.event_seqs == (0,)
    assert "UNVERIFIED" in result.evidence.computation


def test_insufficient_data_fallback_uses_a_real_run_start_seq_when_the_caller_has_one() -> None:
    """A caller with the run's own event log should get real I6 evidence, not a placeholder."""
    result = verdict(comparison=None, agent_count=2, span_count=10, run_start_seq=42)
    assert result.verdict_class is VerdictClass.INSUFFICIENT_DATA
    assert result.evidence.event_seqs == (42,)
    assert "run_start_seq=42" in result.evidence.computation
    assert "UNVERIFIED" not in result.evidence.computation


def test_precedence_unreliable_topology_beats_beneficial() -> None:
    """PRD §18.1's literal precedence.

    Both trigger, `UNRELIABLE_TOPOLOGY` wins the headline, but `BENEFICIAL` is not lost — it
    is reported as a secondary class.
    """
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        resilience=_resilience(score=40),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.UNRELIABLE_TOPOLOGY
    assert VerdictClass.BENEFICIAL in result.secondary_classes


def test_precedence_state_conflict_risk_beats_coordination_bottleneck() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5),
        edge_aggregates=(_edge(cp_share=0.6),),
        state_conflict_findings=(_conflict_finding("critical"),),
        agent_count=2,
        span_count=10,
    )
    assert result.verdict_class is VerdictClass.STATE_CONFLICT_RISK
    assert VerdictClass.COORDINATION_BOTTLENECK in result.secondary_classes


# ---------------------------------------------------------------------------------------------
# The coordination score (PRD §18.2)
# ---------------------------------------------------------------------------------------------


def test_coordination_score_hand_computed() -> None:
    """speedup(40) + efficiency(25) + reliability(25) - conflict_penalty.

    Computed by hand against the exact default weights in `verdict_rules.toml`.
    """
    rules = load_verdict_rules()
    comparison = _comparison(achieved_speedup=1.2, ideal_parallel_speedup=1.2)
    decomposition = _decomposition(productive_ms=80, virtual_makespan_ms=100)  # 80% productive
    resilience = _resilience(score=80)

    result = verdict(
        comparison=comparison,
        decomposition=decomposition,
        resilience=resilience,
        agent_count=2,
        span_count=10,
    )

    speedup_component = 1.0 * rules.speedup_weight  # achieved == ideal -> ratio 1.0, capped
    efficiency_component = 0.8 * rules.efficiency_weight  # 80% productive
    reliability_component = (80 / 100) * rules.reliability_weight
    expected = round(speedup_component + efficiency_component + reliability_component)
    assert result.coordination_score == expected


def test_coordination_score_uses_full_reliability_weight_when_no_chaos_run() -> None:
    """PRD §18.2, verbatim: "25 if no chaos run" — `resilience=None` is not a 0."""
    rules = load_verdict_rules()
    result = verdict(
        comparison=_comparison(achieved_speedup=1.2, ideal_parallel_speedup=1.2),
        decomposition=_decomposition(productive_ms=100, virtual_makespan_ms=100),
        resilience=None,
        agent_count=2,
        span_count=10,
    )
    expected = round(
        1.0 * rules.speedup_weight + 1.0 * rules.efficiency_weight + rules.reliability_weight
    )
    assert result.coordination_score == expected


def test_coordination_score_is_none_without_a_comparison() -> None:
    result = verdict(comparison=None, agent_count=2, span_count=10)
    assert result.coordination_score is None


def test_coordination_score_conflict_penalty_is_capped() -> None:
    """`conflict_penalty_max` (25) caps the deduction even with many high/critical findings."""
    rules = load_verdict_rules()
    many_findings = tuple(_conflict_finding("critical") for _ in range(10))
    with_conflicts = verdict(
        comparison=_comparison(achieved_speedup=1.2, ideal_parallel_speedup=1.2),
        decomposition=_decomposition(productive_ms=100, virtual_makespan_ms=100),
        resilience=_resilience(score=100),
        state_conflict_findings=many_findings,
        agent_count=2,
        span_count=10,
    )
    without_conflicts = verdict(
        comparison=_comparison(achieved_speedup=1.2, ideal_parallel_speedup=1.2),
        decomposition=_decomposition(productive_ms=100, virtual_makespan_ms=100),
        resilience=_resilience(score=100),
        agent_count=2,
        span_count=10,
    )
    assert with_conflicts.coordination_score is not None
    assert without_conflicts.coordination_score is not None
    assert without_conflicts.coordination_score - with_conflicts.coordination_score == round(
        rules.conflict_penalty_max
    )


# ---------------------------------------------------------------------------------------------
# Confidence (PRD §18.5) — never quietly rounded up (Design Constraint 5)
# ---------------------------------------------------------------------------------------------


def test_confidence_high_requires_low_residual_and_grade_a_and_no_gaps() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5, grade=ComparabilityGrade.A),
        decomposition=_decomposition(residual_fraction=0.01),
        agent_count=2,
        span_count=10,
        instrumentation_gap_count=0,
    )
    assert result.confidence is Confidence.HIGH


def test_confidence_medium_on_grade_b() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5, grade=ComparabilityGrade.B),
        decomposition=_decomposition(residual_fraction=0.01),
        agent_count=2,
        span_count=10,
    )
    assert result.confidence is Confidence.MEDIUM


def test_confidence_medium_on_instrumentation_gaps() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5, grade=ComparabilityGrade.A),
        decomposition=_decomposition(residual_fraction=0.01),
        agent_count=2,
        span_count=10,
        instrumentation_gap_count=1,
    )
    assert result.confidence is Confidence.MEDIUM


def test_confidence_low_on_grade_c() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5, grade=ComparabilityGrade.C),
        decomposition=_decomposition(residual_fraction=0.01),
        agent_count=2,
        span_count=10,
    )
    assert result.confidence is Confidence.LOW


def test_confidence_low_on_high_residual_fraction() -> None:
    result = verdict(
        comparison=_comparison(achieved_speedup=1.5, grade=ComparabilityGrade.A),
        decomposition=_decomposition(residual_fraction=0.10),
        agent_count=2,
        span_count=10,
    )
    assert result.confidence is Confidence.LOW


def test_confidence_low_without_a_comparison() -> None:
    result = verdict(comparison=None, agent_count=2, span_count=10)
    assert result.confidence is Confidence.LOW


# ---------------------------------------------------------------------------------------------
# verdict_rules.toml — a threshold change visibly changes the verdict class (Definition of Done)
# ---------------------------------------------------------------------------------------------


def test_a_threshold_change_visibly_changes_the_verdict_class() -> None:
    """The exact Definition-of-Done demonstration.

    Raising `beneficial_min_speedup` turns a previously-BENEFICIAL comparison into NEUTRAL,
    with no other input changed.
    """
    comparison = _comparison(achieved_speedup=1.2)  # >= default 1.15 -> BENEFICIAL
    default_rules = load_verdict_rules()
    baseline_result = verdict(
        comparison=comparison, agent_count=2, span_count=10, rules=default_rules
    )
    assert baseline_result.verdict_class is VerdictClass.BENEFICIAL

    stricter_rules = replace(default_rules, beneficial_min_speedup=10.0)
    changed_result = verdict(
        comparison=comparison, agent_count=2, span_count=10, rules=stricter_rules
    )
    assert changed_result.verdict_class is VerdictClass.NEUTRAL
    # (implied by the two `is` assertions above — NEUTRAL != BENEFICIAL — spelled out anyway as
    # the one line that is this test's whole point)
