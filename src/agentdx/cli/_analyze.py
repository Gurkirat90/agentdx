"""Compose the real analysis pipeline over a sealed event log.

Every function this module calls is a pure function from an already-built, already-tested
`analysis/` submodule (PRD §16-§19); this module's only job is threading their outputs into
each other in the order `analysis/verdict.py`'s own docstring names, and handling the
honest-skip cases (no baseline, no chaos run) the same way those modules' own docstrings
already specify. **No new computation lives here** — this is the "the CLI composes; it must
not implement" mission constraint, applied to the analysis layer specifically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from agentdx.analysis.aggregates import (
    AgentAggregate,
    EdgeAggregate,
    compute_agent_aggregates,
    compute_edge_aggregates,
)
from agentdx.analysis.baseline import BaselineComparison, BaselineRun, compare
from agentdx.analysis.overhead import (
    OverheadDecomposition,
    TotalWorkDecomposition,
    decompose_critical_path,
    decompose_total_work,
)
from agentdx.analysis.race import Finding as RaceFinding
from agentdx.analysis.race import detect_conflicts
from agentdx.analysis.redundancy import RedundancyGroup, detect_redundancy
from agentdx.analysis.resilience import FaultRunInput, ResilienceResult
from agentdx.analysis.resilience import score as score_resilience
from agentdx.analysis.timing import (
    CriticalPathResult,
    ParallelismMetrics,
    TimingDAG,
    build_timing_dag,
    critical_path,
    parallelism_metrics,
)
from agentdx.analysis.verdict import (
    StateConflictFinding,
    Verdict,
    VerdictRules,
    load_verdict_rules,
    verdict,
)
from agentdx.events.schema import Event, EventType

__all__ = ["AnalysisResult", "analyze_events"]


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Every artefact one call to `analyze_events` produced, bundled for display/serialisation."""

    dag: TimingDAG
    critical_path: CriticalPathResult
    parallelism: ParallelismMetrics
    redundancy_groups: tuple[RedundancyGroup, ...]
    decomposition: OverheadDecomposition
    total_work: TotalWorkDecomposition
    edge_aggregates: tuple[EdgeAggregate, ...]
    agent_aggregates: tuple[AgentAggregate, ...]
    race_findings: tuple[RaceFinding, ...]
    comparison: BaselineComparison | None
    resilience: ResilienceResult | None
    verdict: Verdict
    agent_count: int
    span_count: int
    instrumentation_gap_count: int


def _agent_count(dag: TimingDAG) -> int:
    return len({node.agent_id for node in dag.nodes.values() if node.agent_id is not None})


def _run_start_seq(events: Sequence[Event]) -> int | None:
    for event in events:
        if event.type is EventType.RUN_START:
            return event.seq
    return None


def analyze_events(
    events: Sequence[Event],
    *,
    baseline: BaselineRun | None = None,
    fault_runs: Sequence[FaultRunInput] = (),
    rules: VerdictRules | None = None,
) -> AnalysisResult:
    """Run every P10-P12 analyser over `events` and compute the PRD §18 verdict.

    Args:
        events: The complete, sealed event log for one run.
        baseline: A single-agent baseline run to compare against (`analysis.baseline.
            generate_baseline`'s result), or `None` to skip §17 comparison entirely — the
            same "baseline skipped" honesty `E-BASE-002` describes, just decided by the
            caller (this module never fabricates a baseline).
        fault_runs: Fault-injected sibling runs to score for resilience (PRD §19), or empty
            to skip — `verdict()`'s own contract for `resilience=None` is "25 if no chaos
            run" (PRD §18.2), not an error.
        rules: Verdict thresholds; loaded from `analysis/verdict_rules.toml` when omitted.

    Raises:
        TimingAnalysisError, OverheadAnalysisError, BaselineAnalysisError,
        ResilienceAnalysisError, VerdictAnalysisError: propagated from the underlying pure
            analysers — this function adds no new failure modes of its own.
    """
    dag = build_timing_dag(events)
    cp = critical_path(dag)
    parallelism = parallelism_metrics(dag, cp)
    redundancy_groups = detect_redundancy(dag)
    decomposition = decompose_critical_path(dag, cp, events, redundancy_groups=redundancy_groups)
    total_work = decompose_total_work(dag, redundancy_groups)
    edge_aggregates = compute_edge_aggregates(dag, cp, events)
    agent_aggregates = compute_agent_aggregates(dag, cp, events)
    race_findings = detect_conflicts(events)

    comparison: BaselineComparison | None = None
    if baseline is not None:
        comparison = compare(events, baseline, multi_dag=dag, multi_cp=cp)

    resilience: ResilienceResult | None = None
    if fault_runs:
        baseline_events = baseline.events if baseline is not None else events
        resilience = score_resilience(baseline_events, fault_runs)

    resolved_rules = rules if rules is not None else load_verdict_rules()
    agent_count = _agent_count(dag)
    span_count = len(dag.nodes)
    gap_count = sum(1 for e in events if e.type is EventType.INSTRUMENTATION_GAP)

    computed_verdict = verdict(
        comparison=comparison,
        decomposition=decomposition,
        total_work=total_work,
        resilience=resilience,
        edge_aggregates=edge_aggregates,
        agent_aggregates=agent_aggregates,
        redundancy_groups=redundancy_groups,
        # race.Finding structurally satisfies StateConflictFinding (both Protocols check
        # exactly type/severity/evidence_seq — see race.Finding's own docstring); mypy's
        # Protocol attribute matching is invariant on `evidence_seq`'s element type
        # (`tuple[int, ...]` vs `Sequence[int]`), so a cast documents what is already true
        # rather than working around a real mismatch.
        state_conflict_findings=cast("Sequence[StateConflictFinding]", race_findings),
        parallelism=parallelism,
        agent_count=agent_count,
        span_count=span_count,
        instrumentation_gap_count=gap_count,
        run_start_seq=_run_start_seq(events),
        rules=resolved_rules,
    )

    return AnalysisResult(
        dag=dag,
        critical_path=cp,
        parallelism=parallelism,
        redundancy_groups=redundancy_groups,
        decomposition=decomposition,
        total_work=total_work,
        edge_aggregates=edge_aggregates,
        agent_aggregates=agent_aggregates,
        race_findings=race_findings,
        comparison=comparison,
        resilience=resilience,
        verdict=computed_verdict,
        agent_count=agent_count,
        span_count=span_count,
        instrumentation_gap_count=gap_count,
    )
