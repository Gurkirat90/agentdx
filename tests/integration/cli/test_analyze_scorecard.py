"""`agentdx analyze RUN_ID --scorecard` (PRD §44.1's G7 gate).

See `cli/commands/analyze.py`'s own module docstring for why `--scorecard` is what triggers
baseline generation (the same `cli._baseline.CliBaselineExecutor` `compare --baseline` uses)
while a bare `analyze RUN_ID` only re-runs the baseline-free analysers.

Real, end-to-end: `fixtures/code_pipeline` against the real `Scheduler`, twice (multi-agent
run, then the generated single-agent baseline `--scorecard` triggers).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app


def _run_code_pipeline(cli_runner: CliRunner) -> str:
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--seed", "42"])
    assert result.exit_code == _exitcodes.OK, result.output
    for line in result.output.splitlines():
        stripped = line.strip()
        if stripped.startswith("run_id:"):
            return stripped.removeprefix("run_id:").strip()
    msg = f"no run_id line in output:\n{result.output}"
    raise AssertionError(msg)


def test_analyze_scorecard_is_g7s_own_literal_gate_shape(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """PRD §44.1's own G7 gate command, run against a real resolved run_id: exit 0."""
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["analyze", run_id, "--scorecard"])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "Coordination Efficiency" in result.output
    assert "Achieved speedup" in result.output


def test_analyze_without_scorecard_never_generates_a_baseline(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """A bare `analyze RUN_ID` prints the verdict without executing a second graph."""
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["analyze", run_id])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "Coordination Efficiency" not in result.output


def test_analyze_scorecard_missing_run_is_not_found(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    result = cli_runner.invoke(app, ["analyze", "r_nonexistent", "--scorecard"])
    assert result.exit_code == _exitcodes.NOT_FOUND


def test_analyze_scorecard_unregistered_target_is_a_usage_error(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    result = cli_runner.invoke(app, ["run", "research_fanout", "--seed", "42"])
    assert result.exit_code == _exitcodes.OK, result.output
    run_id = next(
        line.strip().removeprefix("run_id:").strip()
        for line in result.output.splitlines()
        if line.strip().startswith("run_id:")
    )
    analyze_result = cli_runner.invoke(app, ["analyze", run_id, "--scorecard"])
    assert analyze_result.exit_code == _exitcodes.USAGE_ERROR
