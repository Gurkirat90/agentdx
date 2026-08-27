"""`--ci` machine-readable output (PRD §22.4) and regression comparison (PRD §22.6).

Exercises `cli.ci`'s JSON/JUnit writers directly against fabricated `CiSummary` objects (the
same unit the mission's DEFINITION OF DONE names: "JUnit XML validates and is consumed by the
shipped GH Actions workflow" — the workflow's own consumption is `.github/workflows/agentdx.yml`
running `actions/upload-artifact` + a JUnit-reporting action against exactly this file shape)
and proves a seeded regression is detected end to end through `check_regression`.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from agentdx.analysis.overhead import OverheadDecomposition
from agentdx.analysis.timing import CriticalPathResult, ParallelismMetrics, TimingDAG
from agentdx.analysis.verdict import Confidence, Evidence, Verdict, VerdictClass
from agentdx.cli import ci as ci_mod
from agentdx.cli._analyze import AnalysisResult
from agentdx.cli._runsummary import CliRunSummary
from agentdx.cli.commands.run import _finish, _metrics_of, _RunOutcome


def _summary(*, achieved_speedup: float, status: str = "passed") -> ci_mod.CiSummary:
    assertion = ci_mod.AssertionOutcome(
        name="speedup_vs_baseline", status=status, expected=">= 1.5", actual=achieved_speedup
    )
    scenario = ci_mod.ScenarioOutcome(
        scenario="kill_reviewer",
        run_id="r_abc12",
        status=status,
        assertions=(assertion,),
        verdict_class="beneficial",
        coordination_score=72,
        confidence="high",
        metrics={"achieved_speedup": achieved_speedup},
    )
    return ci_mod.CiSummary(
        agentdx_version="0.1.0",
        started_at="2026-08-25T00:00:00Z",
        duration_wall_s=12.5,
        scenarios=(scenario,),
    )


def test_write_json_summary_matches_prd_22_4_shape(tmp_path: Path) -> None:
    summary = _summary(achieved_speedup=1.8)
    path = ci_mod.write_json_summary(summary, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == ci_mod.JSON_SCHEMA_VERSION
    assert data["totals"] == {
        "scenarios": 1,
        "passed": 1,
        "failed": 0,
        "assertions": 1,
        "failed_assertions": 0,
    }
    assert data["scenarios"][0]["verdict"]["class"] == "beneficial"


def test_write_junit_xml_is_well_formed_and_carries_one_testcase(tmp_path: Path) -> None:
    summary = _summary(achieved_speedup=1.8)
    path = ci_mod.write_junit_xml(summary, tmp_path)
    root = ET.parse(path).getroot()  # noqa: S314 - our own just-written file, not untrusted
    assert root.tag == "testsuites"
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.get("name") == "kill_reviewer"
    case = suite.find("testcase")
    assert case is not None
    assert case.get("name") == "speedup_vs_baseline"
    assert case.find("failure") is None


def test_write_junit_xml_records_a_failure_element_for_a_failed_assertion(
    tmp_path: Path,
) -> None:
    summary = _summary(achieved_speedup=0.9, status="failed")
    path = ci_mod.write_junit_xml(summary, tmp_path)
    root = ET.parse(path).getroot()  # noqa: S314 - our own just-written file, not untrusted
    case = root.find("testsuite/testcase")
    assert case is not None
    failure = case.find("failure")
    assert failure is not None
    assert "0.9" in (failure.text or "")


def test_check_regression_detects_a_seeded_speedup_regression(tmp_path: Path) -> None:
    """PRD §22.6: a >5% relative drop in `achieved_speedup` is a reported violation."""
    baseline = _summary(achieved_speedup=2.0)
    baseline_path = ci_mod.write_json_summary(baseline, tmp_path / "baseline")
    loaded_baseline = ci_mod.load_summary(baseline_path)

    regressed_current = _summary(achieved_speedup=1.5)  # 25% down — well past the 5% tolerance
    violations = ci_mod.check_regression(regressed_current, loaded_baseline)

    assert len(violations) == 1
    assert violations[0].metric == "achieved_speedup"
    assert violations[0].scenario == "kill_reviewer"


def test_check_regression_passes_when_within_tolerance(tmp_path: Path) -> None:
    baseline = _summary(achieved_speedup=2.0)
    baseline_path = ci_mod.write_json_summary(baseline, tmp_path / "baseline")
    loaded_baseline = ci_mod.load_summary(baseline_path)

    current = _summary(achieved_speedup=1.97)  # 1.5% down — within the 5% relative tolerance
    violations = ci_mod.check_regression(current, loaded_baseline)

    assert violations == ()


def test_check_regression_flags_new_high_severity_findings(tmp_path: Path) -> None:
    baseline_scenario = ci_mod.ScenarioOutcome(
        scenario="kill_reviewer",
        run_id="r_base1",
        status="passed",
        assertions=(),
        metrics={"findings_high_plus": 0},
    )
    baseline = ci_mod.CiSummary("0.1.0", "", 0.0, (baseline_scenario,))
    current_scenario = ci_mod.ScenarioOutcome(
        scenario="kill_reviewer",
        run_id="r_cur1",
        status="passed",
        assertions=(),
        metrics={"findings_high_plus": 2},
    )
    current = ci_mod.CiSummary("0.1.0", "", 0.0, (current_scenario,))

    violations = ci_mod.check_regression(current, baseline)
    assert len(violations) == 1
    assert violations[0].metric == "findings[high+]"


# ---------------------------------------------------------------------------------------
# Regression test: PRD §22.6's `coordination_score` tolerance was silently unreachable.
#
# `check_regression` (above) has always read every tolerance from `ScenarioOutcome.metrics`,
# a plain dict built by `commands.run._metrics_of`. That function's real production path never
# requested `"coordination_score"` from `CliRunSummary.metric()`, and `metric()` itself had no
# branch for that name either — so a real `coordination_score` regression could never be
# detected, even though `REGRESSION_TOLERANCES` above declares a bound for it and every test
# above this line proves the *comparison* logic is correct once given the number. These three
# tests exercise the real chain the ones above skip: `CliRunSummary.metric()` ->
# `commands.run._metrics_of()` -> `commands.run._finish()`'s written JSON — using real
# `AnalysisResult`/`Verdict` objects, not a hand-typed `ScenarioOutcome`.
# ---------------------------------------------------------------------------------------


def _analysis_result(*, coordination_score: int | None) -> AnalysisResult:
    """A minimal-but-valid `AnalysisResult` — every field a real instance of its real type.

    Never a mock, following the same "minimal-but-valid factory" pattern
    `tests/analysis/test_verdict.py` uses for `verdict()`'s own inputs. Only `verdict` carries
    the one value these tests care about.
    """
    dag = TimingDAG(
        run_id="r_test01", nodes={}, edges={}, topological_order=(), virtual_makespan_ms=0
    )
    bucket_ms = {
        "retry_recovery": 0,
        "redundant_work": 0,
        "orchestration": 0,
        "productive_work": 0,
        "handoff": 0,
        "blocking_wait": 0,
    }
    bucket_evidence_seq = dict.fromkeys(bucket_ms, ())
    decomposition = OverheadDecomposition(
        bucket_ms=bucket_ms,
        bucket_evidence_seq=bucket_evidence_seq,
        residual_ms=0,
        residual_fraction=0.0,
        residual_flagged=False,
        residual_tolerance=0.02,
        virtual_makespan_ms=0,
        critical_path_length_ms=0,
    )
    verdict = Verdict(
        verdict_class=VerdictClass.BENEFICIAL,
        secondary_classes=(),
        coordination_score=coordination_score,
        confidence=Confidence.HIGH,
        findings=(),
        recommendations=(),
        evidence=Evidence(event_seqs=(1,), spans=(), computation="test fixture"),
    )
    return AnalysisResult(
        dag=dag,
        critical_path=CriticalPathResult(path=(), length_ms=0),
        parallelism=ParallelismMetrics(
            total_work_ms=0, critical_path_length_ms=0, average_parallelism=0.0
        ),
        redundancy_groups=(),
        decomposition=decomposition,
        total_work=decomposition,
        edge_aggregates=(),
        agent_aggregates=(),
        race_findings=(),
        comparison=None,
        resilience=None,
        verdict=verdict,
        agent_count=0,
        span_count=0,
        instrumentation_gap_count=0,
    )


def test_cli_run_summary_metric_recognises_coordination_score() -> None:
    """`metric("coordination_score")` reads `analysis.verdict.coordination_score`.

    Previously: no branch existed, so this always fell through to `None`.
    """
    summary = CliRunSummary(
        run_id="r_test01",
        analysis=_analysis_result(coordination_score=61),
        faults_fired=0,
        success_check_passed=True,
        deterministic_replay_verified=None,
    )
    assert summary.metric("coordination_score") == 61.0


def test_cli_run_summary_metric_coordination_score_is_none_without_a_verdict_score() -> None:
    """A `Verdict` with no comparison (`coordination_score=None`) stays `None`.

    Never a fabricated 0 — same discipline `AGENTS.md` §2 requires everywhere else.
    """
    summary = CliRunSummary(
        run_id="r_test01",
        analysis=_analysis_result(coordination_score=None),
        faults_fired=0,
        success_check_passed=True,
        deterministic_replay_verified=None,
    )
    assert summary.metric("coordination_score") is None


def test_metrics_of_includes_coordination_score() -> None:
    """`_metrics_of` — the function `_finish` calls to build the dict `check_regression` reads.

    It must request `"coordination_score"` — previously the name was absent from its
    extraction tuple, so the key never appeared in `ScenarioOutcome.metrics` even once
    `metric()` itself was fixed.
    """
    outcome = _RunOutcome(
        scenario_name="kill_reviewer",
        run_id="r_test01",
        status="passed",
        exit_code=0,
        assertions=(),
        analysis=_analysis_result(coordination_score=61),
        summary=CliRunSummary(
            run_id="r_test01",
            analysis=_analysis_result(coordination_score=61),
            faults_fired=0,
            success_check_passed=True,
            deterministic_replay_verified=None,
        ),
    )
    metrics = _metrics_of(outcome)
    assert metrics.get("coordination_score") == 61.0


def test_finish_ci_json_summary_carries_coordination_score(tmp_path: Path) -> None:
    """End to end through `_finish`'s real `--ci` path.

    No baseline comparison, so no `DeadlockError`/scheduler involvement is needed to exercise
    this: the written `summary.json`'s `verdict.coordination_score` and
    `metrics.coordination_score` must both be the real score, not `null`/absent, the way a
    real `agentdx run --ci` output would be today without this fix.
    """
    from agentdx.cli._output import Output

    outcome = _RunOutcome(
        scenario_name="kill_reviewer",
        run_id="r_test01",
        status="passed",
        exit_code=0,
        assertions=(),
        analysis=_analysis_result(coordination_score=61),
        summary=CliRunSummary(
            run_id="r_test01",
            analysis=_analysis_result(coordination_score=61),
            faults_fired=0,
            success_check_passed=True,
            deterministic_replay_verified=None,
        ),
    )
    exit_code = _finish(
        [outcome],
        ci=True,
        ci_format="json",
        out_dir=tmp_path,
        baseline_run=None,
        out=Output(json_mode=True, quiet=True, verbose=False, no_color=True),
    )
    assert exit_code == 0
    data = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    scenario = data["scenarios"][0]
    assert scenario["verdict"]["coordination_score"] == 61
    assert scenario["metrics"]["coordination_score"] == 61.0
