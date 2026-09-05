"""`agentdx compare RUN_ID --baseline` (PRD §44.1's G6 gate).

See `cli/commands/compare.py`'s own module docstring for the full account of how the PRD
§37.1 two-run form and G6's one-run-plus-flag form share one command, and `cli/_baseline.py`
for how the generated baseline is actually executed (a real, scripted single-agent run
through the real `Scheduler`/`CliRunHost`, not a fake).

Real, end-to-end: `fixtures/code_pipeline` against the real `Scheduler`, twice — once for the
multi-agent run, once (inside `compare`) for the generated baseline.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app


def _run_code_pipeline(cli_runner: CliRunner) -> str:
    """Execute `code_pipeline` for real and return its run_id."""
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--seed", "42"])
    assert result.exit_code == _exitcodes.OK, result.output
    for line in result.output.splitlines():
        stripped = line.strip()
        if stripped.startswith("run_id:"):
            return stripped.removeprefix("run_id:").strip()
    msg = f"no run_id line in output:\n{result.output}"
    raise AssertionError(msg)


def test_compare_baseline_is_g6s_own_literal_gate_shape(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """PRD §44.1's own G6 gate command, run against a real resolved run_id: exit 0."""
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id, "--baseline"])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "Coordination Efficiency" in result.output
    assert "Comparability" in result.output


def test_compare_baseline_reuses_the_multi_runs_own_tool_calls(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """The scripted baseline hits the multi-run's own committed tool-response pool for real."""
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id, "--baseline"])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "cache reuse 100%" in result.output


def test_compare_requires_run_id_b_or_baseline(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id])
    assert result.exit_code == _exitcodes.USAGE_ERROR


def test_compare_rejects_both_run_id_b_and_baseline(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id, run_id, "--baseline"])
    assert result.exit_code == _exitcodes.USAGE_ERROR


def test_compare_two_runs_diffs_directly_with_no_baseline_generation(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """PRD §37.1's own two-run form: no baseline is generated, just a direct diff."""
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id, run_id])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "verdict" in result.output


def test_compare_two_runs_discloses_that_the_exit_code_never_reflects_a_regression(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """OP-2 first-pass finding #3 against `cli/` (`op2-audit-p17.md`).

    No regression/tolerance logic exists for the two-run form — PRD §37.1 documents exit 1
    for "regression beyond tolerance", but that is unreachable here. Rather than leave this
    silent, the command now warns on every invocation, matching `--tolerance-file`/`--force`'s
    own disclosure above.
    """
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id, run_id])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "informational only" in result.output
    assert "exit code is always 0" in result.output


def test_compare_two_runs_findings_count_includes_verdict_layer_findings(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """The two-run form's `findings` row must not undercount to race findings alone.

    `code_pipeline` (seed 42) has one `state_conflict` finding and one `redundancy`
    finding — two real findings total, not one (OP-2 first-pass finding #2's own
    informational-display instance, `op2-audit-p17.md` §2).
    """
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["compare", run_id, run_id])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "findings      2" in result.output


def test_compare_baseline_missing_run_is_not_found(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    result = cli_runner.invoke(app, ["compare", "r_nonexistent", "--baseline"])
    assert result.exit_code == _exitcodes.NOT_FOUND


def test_compare_baseline_unregistered_target_is_a_usage_error(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """`research_fanout` has no registered baseline executor — fails honestly, not silently."""
    result = cli_runner.invoke(app, ["run", "research_fanout", "--seed", "42"])
    assert result.exit_code == _exitcodes.OK, result.output
    run_id = next(
        line.strip().removeprefix("run_id:").strip()
        for line in result.output.splitlines()
        if line.strip().startswith("run_id:")
    )
    compare_result = cli_runner.invoke(app, ["compare", run_id, "--baseline"])
    assert compare_result.exit_code == _exitcodes.USAGE_ERROR
