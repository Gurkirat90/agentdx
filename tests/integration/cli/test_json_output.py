"""Global `--json` (PRD §37.3) on `run`, `analyze`, `compare`, `scenario run`.

OP-2 first-pass finding #1 against `cli/` (`op2-audit-p17.md`): before this fix, all four
commands silently emitted nothing to stdout under `--json` — all output went to stderr
regardless of the flag, and `agentdx run fixtures/code_pipeline --json | jq .verdict.class`
(the exact example `cli/_output.py`'s own module docstring uses to explain why `--json`
exists) returned nothing to pipe into `jq` at all. `_output.Output.line()` already correctly
routed human prose to stderr under `json_mode` — what was missing was ever writing the JSON
object itself.

`typer.testing.CliRunner` (Click 8.5+) exposes `result.stdout`/`result.stderr` as genuinely
separate streams (not merged into one `.output`), so these tests assert directly on the
contract: stdout parses as JSON and nothing else, human text is on stderr.
"""

from __future__ import annotations

import json
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


def test_run_json_emits_a_parseable_object_to_stdout_only(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    result = cli_runner.invoke(app, ["--json", "run", "code_pipeline", "--seed", "42"])
    assert result.exit_code == _exitcodes.OK
    payload = json.loads(result.stdout)
    assert payload["scenarios"][0]["scenario"] == "code_pipeline"
    assert payload["scenarios"][0]["status"] == "passed"
    # Human prose still goes to stderr, unchanged — only the missing stdout object was the bug.
    assert "code_pipeline: passed" in result.stderr


def test_analyze_json_emits_a_parseable_object_to_stdout_only(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["--json", "analyze", run_id])
    assert result.exit_code == _exitcodes.OK
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["verdict_class"] == "state_conflict_risk"
    assert result.stderr  # human report still went somewhere


def test_compare_baseline_json_emits_a_parseable_object_to_stdout_only(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["--json", "compare", run_id, "--baseline"])
    assert result.exit_code == _exitcodes.OK
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert "achieved_speedup" in payload


def test_compare_two_run_json_emits_a_parseable_object_to_stdout_only(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    run_id = _run_code_pipeline(cli_runner)
    result = cli_runner.invoke(app, ["--json", "compare", run_id, run_id])
    assert result.exit_code == _exitcodes.OK
    payload = json.loads(result.stdout)
    assert payload["run_id_a"] == payload["run_id_b"] == run_id
    assert payload["regression_evaluated"] is False


def test_scenario_run_json_emits_a_parseable_object_to_stdout_only(
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    scenario = repo_root / "scenarios" / "kill_reviewer.yaml"
    result = cli_runner.invoke(app, ["--json", "scenario", "run", str(scenario), "--repeat", "1"])
    assert result.exit_code in (_exitcodes.OK, _exitcodes.ASSERTION_FAILURE), result.stderr
    payload = json.loads(result.stdout)
    assert payload["repeat"] == 1
    assert len(payload["runs"]) == 1
