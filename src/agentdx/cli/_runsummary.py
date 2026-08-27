"""A concrete `scenario.assertions.RunSummary`.

The producer that module's own docstring says belongs to "the not-yet-built api/analysis
surface" (P10/P11 at the time it was written). `scenario/`'s own module docstring is explicit
that this is intentional: `RunSummary` is a
`Protocol` specifically so a producer could be built later without `scenario/` importing it.
This is that producer, built from an `AnalysisResult` (`cli._analyze`) plus the handful of
facts only a `RunHost` execution knows (`faults_fired`, `success_check_passed`,
`deterministic_replay_verified`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from agentdx.analysis.race import Finding as RaceFinding
from agentdx.cli._analyze import AnalysisResult

__all__ = ["CliRunSummary"]


def _edge_share(result: AnalysisResult, edge: str) -> float | None:
    src, _, dst = edge.partition("->")
    for agg in result.edge_aggregates:
        if agg.src_agent_id == src and agg.dst_agent_id == dst:
            return agg.cp_share
    return None


@dataclass(frozen=True, slots=True)
class CliRunSummary:
    """Structurally satisfies `scenario.assertions.RunSummary` (`AGENTS.md` §2: no re-typing).

    One instance per evaluated run. `metric()` recognises the names that Protocol's own
    docstring lists, plus `coordination_score` (PRD §22.4/§22.6's verdict/CI-regression
    metric — not part of the built-in-assertion vocabulary, so deliberately not added to the
    Protocol's own "recognised names" list) and the `critical_path_share:<edge>` family —
    every other name returns `None` ("not measurable"), never a fabricated value.
    """

    run_id: str
    analysis: AnalysisResult
    faults_fired: int
    success_check_passed: bool | None
    deterministic_replay_verified: bool | None
    findings: Sequence[RaceFinding] = field(default_factory=tuple)

    def metric(self, name: str) -> float | str | None:
        """Return a named scorecard/verdict metric, or `None` if not (yet) computable."""
        comparison = self.analysis.comparison
        if name == "speedup_vs_baseline":
            return comparison.achieved_speedup if comparison is not None else None
        if name == "resilience_score":
            resilience = self.analysis.resilience
            if resilience is None or resilience.resilience_score is None:
                return None
            return float(resilience.resilience_score)
        if name == "coordination_score":
            score = self.analysis.verdict.coordination_score
            return float(score) if score is not None else None
        if name == "token_cost_multiplier":
            return comparison.token_cost_multiplier if comparison is not None else None
        if name == "comparability_grade":
            return comparison.comparability.grade.value if comparison is not None else None
        if name.startswith("critical_path_share:"):
            edge = name.removeprefix("critical_path_share:")
            return _edge_share(self.analysis, edge)
        return None
