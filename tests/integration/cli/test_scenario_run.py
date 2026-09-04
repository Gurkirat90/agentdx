"""`agentdx scenario run PATH --repeat N` (PRD §44.1's G4 gate).

See `cli/commands/scenario_run.py`'s own module docstring for the full account of why this
CLI-invented subcommand exists with no formal PRD §37.1 entry.

Real, end-to-end: `scenarios/kill_reviewer.yaml` against the real `Scheduler`, the real
`CrashInjector` fault hook, and the real `code_pipeline` fixture — as of D-62 task #25's
candidate beta, this fixture completes end-to-end for real (`test_exit_codes.py`'s own
module docstring documents the same fact).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app


def test_scenario_run_kill_reviewer_reproduces_identically_across_repeats(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    """PRD §44.1's own G4 gate command, run literally (a smaller `--repeat` for test speed)."""
    scenario_path = repo_root / "scenarios" / "kill_reviewer.yaml"
    result = cli_runner.invoke(app, ["scenario", "run", str(scenario_path), "--repeat", "5"])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "5/5 repeats produced an identical outcome" in result.output
    assert result.output.count("status=passed") == 5


def test_scenario_run_defaults_to_one_repeat(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    """`--repeat` is optional; omitting it runs the scenario exactly once."""
    scenario_path = repo_root / "scenarios" / "kill_reviewer.yaml"
    result = cli_runner.invoke(app, ["scenario", "run", str(scenario_path)])
    assert result.exit_code == _exitcodes.OK, result.output
    assert "1/1 repeats produced an identical outcome" in result.output


def test_scenario_run_rejects_repeat_below_one(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    scenario_path = repo_root / "scenarios" / "kill_reviewer.yaml"
    result = cli_runner.invoke(app, ["scenario", "run", str(scenario_path), "--repeat", "0"])
    assert result.exit_code == _exitcodes.USAGE_ERROR


def test_scenario_run_missing_file_is_not_found(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    result = cli_runner.invoke(app, ["scenario", "run", str(tmp_path / "no_such_scenario.yaml")])
    assert result.exit_code == _exitcodes.NOT_FOUND


def test_scenario_run_each_repeat_gets_its_own_isolated_store(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    """Repeats must be genuine re-executions, not D-80's reuse-and-print against one store.

    If repeats shared a store, repeat 2..N would hit `RunAlreadyExistsError` (the identical
    seed produces the identical `run_id`) and be silently rescored from repeat 1's own stored
    log rather than re-run — making a "N/N identical" claim vacuous. Each repeat's `Store`
    lives in its own throwaway temp dir, never `isolated_data_dir` — this asserts that
    directly: the command's own configured data dir gains no `agentdx.db` at all.
    """
    scenario_path = repo_root / "scenarios" / "kill_reviewer.yaml"
    result = cli_runner.invoke(app, ["scenario", "run", str(scenario_path), "--repeat", "3"])
    assert result.exit_code == _exitcodes.OK, result.output

    store_path = isolated_data_dir / "agentdx.db"
    assert not store_path.exists(), (
        "scenario run wrote to the user's real data dir instead of an isolated per-repeat "
        f"store: {store_path}"
    )
