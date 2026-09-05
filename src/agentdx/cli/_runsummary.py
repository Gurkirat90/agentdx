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
from typing import cast

from agentdx.cli._analyze import AnalysisResult
from agentdx.scenario.assertions import Finding

__all__ = ["CliRunSummary", "combined_findings"]


@dataclass(frozen=True, slots=True)
class _AdaptedVerdictFinding:
    """Adapts one `analysis.verdict.VerdictFinding` to `scenario.assertions.Finding`'s shape.

    `VerdictFinding` does not structurally satisfy that `Protocol` as-is: `severity` is a
    `Severity` enum there, not a bare `str`, and its evidence lives at `evidence.event_seqs`,
    not a flat `evidence_seq` attribute — unlike `analysis.race.Finding`, which already
    matches the Protocol's shape with no adapter needed. OP-2 first-pass finding #2 against
    `cli/` (`op2-audit-p17.md`): before this adapter existed, `CliRunSummary.findings` (below)
    was built from `analysis.race_findings` alone, so `coordination_bottleneck`/`redundancy`
    findings — real, computed by `analysis/verdict.py`, and explicitly listed as known,
    producible types in `scenario.assertions._KNOWN_FINDING_TYPES` — could never reach
    anything that reads `RunSummary.findings` (`--assert findings.<type>`, the scenario-file
    `max_findings` built-in, `scenario run --repeat`'s reproducibility signature). A naive
    `tuple(race_findings) + tuple(verdict_findings)` concatenation would fail the Protocol's
    `runtime_checkable` shape check the moment anything iterated it expecting `.evidence_seq`
    — this adapter is what makes the concatenation actually satisfy `Finding` uniformly.
    """

    type: str
    severity: str
    evidence_seq: tuple[int, ...]


def combined_findings(result: AnalysisResult) -> tuple[Finding, ...]:
    """Return every finding this build's analysis layer produced for one run, uniformly typed.

    `analysis.race_findings` (already `Finding`-shaped) plus `analysis.verdict.findings`
    (`coordination_bottleneck`/`redundancy`, adapted via `_AdaptedVerdictFinding` above) — the
    complete set `scenario.assertions._KNOWN_FINDING_TYPES` advertises as real. Used by both
    `cli.commands.run._score_one`/`_score_reused` to build `CliRunSummary.findings`, so every
    consumer of a `RunSummary` sees the same, complete picture rather than the race-only
    subset a caller might otherwise build by hand.
    """
    # Both `analysis.race.Finding` and `_AdaptedVerdictFinding` are frozen dataclasses;
    # `Finding`'s bare-attribute `Protocol` declaration implies a settable variable to mypy's
    # static checker even though a read-only frozen field satisfies it at runtime
    # (`runtime_checkable` — this Protocol is only ever read from, never assigned to). Cast,
    # not restructure — matching `cli/_analyze.py`'s own precedent
    # (`cast("Sequence[StateConflictFinding]", race_findings)`) for the identical
    # Protocol-vs-frozen-dataclass gap.
    adapted = cast(
        "tuple[Finding, ...]",
        tuple(
            _AdaptedVerdictFinding(
                type=f.type, severity=f.severity.value, evidence_seq=f.evidence.event_seqs
            )
            for f in result.verdict.findings
        ),
    )
    race = cast("tuple[Finding, ...]", tuple(result.race_findings))
    return race + adapted


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

    `findings` should be built via `combined_findings(analysis)` above, not
    `analysis.race_findings` alone — see that function's docstring (OP-2 first-pass finding #2
    against `cli/`, `op2-audit-p17.md`).
    """

    run_id: str
    analysis: AnalysisResult
    faults_fired: int
    success_check_passed: bool | None
    deterministic_replay_verified: bool | None
    findings: Sequence[Finding] = field(default_factory=tuple)

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
