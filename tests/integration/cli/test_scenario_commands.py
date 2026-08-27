"""`agentdx scenario validate|list|expand` (PRD §37.1) against the real, shipped scenarios."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app


def test_scenario_validate_passes_on_a_real_scenario(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    result = cli_runner.invoke(app, ["scenario", "validate", "scenarios/kill_reviewer.yaml"])
    assert result.exit_code == _exitcodes.OK, result.stdout


def test_scenario_validate_reports_usage_error_on_a_malformed_document(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("scenario: missing_required_task_field\n", encoding="utf-8")
    result = cli_runner.invoke(app, ["scenario", "validate", str(bad)])
    assert result.exit_code == _exitcodes.USAGE_ERROR


def test_scenario_list_finds_the_shipped_scenarios(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    result = cli_runner.invoke(app, ["--json", "scenario", "list", "scenarios/"])
    assert result.exit_code == _exitcodes.OK
    assert "kill_reviewer" in result.stdout


def test_scenario_expand_reports_no_matrix_for_a_non_matrix_scenario(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    result = cli_runner.invoke(app, ["scenario", "expand", "scenarios/kill_reviewer.yaml"])
    assert result.exit_code == _exitcodes.OK
    assert "no `matrix:` block" in result.stdout
