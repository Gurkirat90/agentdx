"""Machine-readable output (PRD §22.4) and regression comparison (PRD §22.6) for `--ci` mode.

Two artefacts, both written to `--out` (default `.agentdx/ci`): a JUnit XML file (one
`<testsuite>` per scenario, one `<testcase>` per assertion — for native CI rendering) and a
JSON summary (`schema_version`-stamped, stable — PRD §22.4's own worked example is this
module's schema, byte for byte).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from xml.dom import minidom

__all__ = [
    "JSON_SCHEMA_VERSION",
    "REGRESSION_TOLERANCES",
    "AssertionOutcome",
    "CiSummary",
    "RegressionViolation",
    "ScenarioOutcome",
    "check_regression",
    "load_summary",
    "write_json_summary",
    "write_junit_xml",
]

JSON_SCHEMA_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class AssertionOutcome:
    """One assertion's evaluated result, in PRD §22.4's `scenarios[].assertions[]` shape."""

    name: str
    status: str
    expected: object
    actual: object
    event_seqs: tuple[int, ...] = ()
    finding_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the PRD §22.4 JSON shape for one assertion outcome."""
        out: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
        }
        if self.event_seqs or self.finding_ids:
            out["evidence"] = {
                "event_seqs": list(self.event_seqs),
                "finding_ids": list(self.finding_ids),
            }
        return out


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """One scenario's full outcome — PRD §22.4's `scenarios[]` entry."""

    scenario: str
    run_id: str | None
    status: str
    assertions: tuple[AssertionOutcome, ...]
    verdict_class: str | None = None
    coordination_score: int | None = None
    confidence: str | None = None
    bundle: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    reused: bool = False
    """D-80/C-34 (`d78-plan.md` §6): a `run_id` collision against an already-sealed run is
    reused rather than re-executed. Not one of PRD §22.4's own named top-level keys, same
    class of deliberate addition as `metrics` above — omitted from the JSON entirely when
    `False` (every scenario before D-80 existed) so an existing `--baseline-run` consumer's
    parsing is unaffected; present and `true` only for a run this invocation reused, so a
    CI consumer diffing two summaries can tell "this re-ran" from "this was already known"
    rather than conflating the two."""

    def as_dict(self) -> dict[str, object]:
        """Return the PRD §22.4 JSON shape for one scenario."""
        out: dict[str, object] = {
            "scenario": self.scenario,
            "run_id": self.run_id,
            "status": self.status,
            "assertions": [a.as_dict() for a in self.assertions],
        }
        if self.verdict_class is not None:
            out["verdict"] = {
                "class": self.verdict_class,
                "coordination_score": self.coordination_score,
                "confidence": self.confidence,
            }
        if self.bundle is not None:
            out["bundle"] = self.bundle
        if self.reused:
            out["reused"] = True
        if self.metrics:
            # Not one of PRD §22.4's own named top-level keys, but load-bearing for §22.6:
            # `check_regression` reads `scenario.metrics` back off a *round-tripped*
            # `--baseline-run` file (`load_summary`), so a summary that dropped this on
            # write would silently defeat every regression check reading it back — caught
            # by `tests/integration/cli/test_ci_mode.py::
            # test_check_regression_detects_a_seeded_speedup_regression`, which failed
            # until this key was added.
            out["metrics"] = dict(self.metrics)
        return out


@dataclass(frozen=True, slots=True)
class CiSummary:
    """The full `--ci` run summary — PRD §22.4's top-level JSON object."""

    agentdx_version: str
    started_at: str
    duration_wall_s: float
    scenarios: tuple[ScenarioOutcome, ...]

    @property
    def totals(self) -> dict[str, int]:
        """Return PRD §22.4's `totals` block, computed from `scenarios`."""
        passed = sum(1 for s in self.scenarios if s.status == "passed")
        failed = sum(1 for s in self.scenarios if s.status != "passed")
        all_assertions = [a for s in self.scenarios for a in s.assertions]
        failed_assertions = sum(1 for a in all_assertions if a.status == "failed")
        return {
            "scenarios": len(self.scenarios),
            "passed": passed,
            "failed": failed,
            "assertions": len(all_assertions),
            "failed_assertions": failed_assertions,
        }

    def as_dict(self) -> dict[str, object]:
        """Return the complete PRD §22.4 JSON object."""
        return {
            "agentdx_version": self.agentdx_version,
            "schema_version": JSON_SCHEMA_VERSION,
            "started_at": self.started_at,
            "duration_wall_s": self.duration_wall_s,
            "scenarios": [s.as_dict() for s in self.scenarios],
            "totals": self.totals,
        }


def write_json_summary(summary: CiSummary, out_dir: Path) -> Path:
    """Write `summary` as `<out_dir>/summary.json`; return the path written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / "summary.json"
    destination.write_text(
        json.dumps(summary.as_dict(), indent=2, sort_keys=False), encoding="utf-8"
    )
    return destination


def load_summary(path: Path) -> CiSummary:
    """Load a previously written JSON summary (e.g. a `--baseline-run` file).

    Accepts either a full summary object (`{"scenarios": [...], ...}`) or a bare
    `{"scenario_name": {"achieved_speedup": ..., ...}, ...}` metrics map — `agentdx baseline
    update` writes the former; a hand-authored tolerance file may use the latter, folded into
    scenarios with no assertions so `check_regression` can read `metrics` uniformly.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if "scenarios" in data:
        scenarios = tuple(
            ScenarioOutcome(
                scenario=s["scenario"],
                run_id=s.get("run_id"),
                status=s["status"],
                assertions=(),
                verdict_class=(s.get("verdict") or {}).get("class"),
                coordination_score=(s.get("verdict") or {}).get("coordination_score"),
                confidence=(s.get("verdict") or {}).get("confidence"),
                metrics=s.get("metrics", {}),
            )
            for s in data["scenarios"]
        )
        return CiSummary(
            agentdx_version=data.get("agentdx_version", "unknown"),
            started_at=data.get("started_at", ""),
            duration_wall_s=data.get("duration_wall_s", 0.0),
            scenarios=scenarios,
        )
    scenarios = tuple(
        ScenarioOutcome(scenario=name, run_id=None, status="passed", assertions=(), metrics=metrics)
        for name, metrics in data.items()
    )
    return CiSummary(
        agentdx_version="unknown", started_at="", duration_wall_s=0.0, scenarios=scenarios
    )


def write_junit_xml(summary: CiSummary, out_dir: Path) -> Path:
    """Write `summary` as JUnit XML at `<out_dir>/junit.xml`; return the path written.

    One `<testsuite>` per scenario, one `<testcase>` per assertion, `<failure>` carrying
    expected/actual and the bundle path (PRD §22.4).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    root = ET.Element("testsuites", {"name": "agentdx", "tests": str(len(summary.totals))})
    for scenario in summary.scenarios:
        suite = ET.SubElement(
            root,
            "testsuite",
            {
                "name": scenario.scenario,
                "tests": str(len(scenario.assertions)),
                "failures": str(sum(1 for a in scenario.assertions if a.status == "failed")),
            },
        )
        if scenario.run_id is not None:
            suite.set("id", scenario.run_id)
        for assertion in scenario.assertions:
            case = ET.SubElement(
                suite,
                "testcase",
                {"name": assertion.name, "classname": scenario.scenario},
            )
            if assertion.status == "failed":
                failure = ET.SubElement(
                    case,
                    "failure",
                    {"message": f"expected {assertion.expected!r}, got {assertion.actual!r}"},
                )
                bundle_note = f"\nreproduce: {scenario.bundle}" if scenario.bundle else ""
                failure.text = (
                    f"expected: {assertion.expected!r}\nactual:   {assertion.actual!r}{bundle_note}"
                )
            elif assertion.status == "not_measurable":
                ET.SubElement(case, "skipped", {"message": "not measurable"})
    rough = ET.tostring(root, encoding="unicode")
    # `rough` is our own just-built ElementTree serialised in-process, not external/untrusted
    # input — minidom's XML-bomb exposure (the reason S318 exists) does not apply here.
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")  # noqa: S318
    destination = out_dir / "junit.xml"
    destination.write_text(pretty, encoding="utf-8")
    return destination


# ---------------------------------------------------------------------------------------
# PRD §22.6 — regression comparison
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegressionViolation:
    """One metric that regressed beyond its tolerance, for one scenario."""

    scenario: str
    metric: str
    baseline: float
    current: float
    tolerance: str
    detail: str


REGRESSION_TOLERANCES: Final[dict[str, tuple[str, float]]] = {
    "achieved_speedup": ("relative_min", -0.05),
    "resilience_score": ("absolute_min", -3.0),
    "coordination_score": ("absolute_min", -5.0),
    "token_cost_multiplier": ("relative_max", 0.10),
}
"""PRD §22.6's table, minus `findings[high+]` (handled separately — it is a count, not a
metric with a tolerance shape the other four share)."""


def _violates(kind: str, bound: float, *, baseline: float, current: float) -> bool:
    if kind == "relative_min":
        return current < baseline * (1 + bound)
    if kind == "absolute_min":
        return current < baseline + bound
    if kind == "relative_max":
        return current > baseline * (1 + bound)
    msg = f"unknown tolerance kind {kind!r}"
    raise ValueError(msg)


def check_regression(
    current: CiSummary,
    baseline: CiSummary,
    *,
    tolerances: dict[str, tuple[str, float]] | None = None,
) -> tuple[RegressionViolation, ...]:
    """Compare `current` against `baseline` per PRD §22.6; return every violation found.

    Baselines are refreshed only by an explicit `agentdx baseline update` — never here, never
    automatically (PRD §22.6: "an auto-refreshing baseline silently ratchets regressions in").
    """
    bounds = tolerances if tolerances is not None else REGRESSION_TOLERANCES
    baseline_by_name = {s.scenario: s for s in baseline.scenarios}
    violations: list[RegressionViolation] = []
    for scenario in current.scenarios:
        base = baseline_by_name.get(scenario.scenario)
        if base is None:
            continue
        for metric, (kind, bound) in bounds.items():
            base_value = base.metrics.get(metric)
            current_value = scenario.metrics.get(metric)
            if base_value is None or current_value is None:
                continue
            if _violates(kind, bound, baseline=base_value, current=current_value):
                violations.append(
                    RegressionViolation(
                        scenario=scenario.scenario,
                        metric=metric,
                        baseline=base_value,
                        current=current_value,
                        tolerance=f"{kind} {bound}",
                        detail=(
                            f"{metric}: baseline={base_value:.4g} current={current_value:.4g} "
                            f"(tolerance {kind} {bound})"
                        ),
                    )
                )
        base_findings = int(base.metrics.get("findings_high_plus", 0))
        current_findings = int(scenario.metrics.get("findings_high_plus", 0))
        if current_findings > base_findings:
            violations.append(
                RegressionViolation(
                    scenario=scenario.scenario,
                    metric="findings[high+]",
                    baseline=float(base_findings),
                    current=float(current_findings),
                    tolerance="0 new",
                    detail=(
                        f"findings[high+]: baseline={base_findings} current={current_findings} "
                        "(tolerance: 0 new)"
                    ),
                )
            )
    return tuple(violations)
