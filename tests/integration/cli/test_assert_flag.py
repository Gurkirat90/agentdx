"""`agentdx run --assert PATH OP VALUE` (CLI-invented, PRD §44.1's G1 gate).

See module docstrings in `cli/commands/run.py::_parse_assert_expr` and
`scenario/assertions.py::eval_findings_type_count` for the full account of why this flag
exists with no PRD §37.1 grammar behind it.

This is a real, end-to-end run against `fixtures/code_pipeline` — the fixture's own seeded
`state_conflict/write_write` defect on `draft.module_a` (PRD §23.1) is what `findings.race`
(an alias for `Finding.type == "state_conflict"`) actually counts. Not monkeypatched: as of
D-62 task #25's candidate beta, `code_pipeline` completes end-to-end for real under the real
`Scheduler` (`test_exit_codes.py`'s own module docstring documents the same fact).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app


def test_assert_findings_race_passes_when_the_seeded_race_is_found(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """PRD §44.1's own G1 gate command, run literally: exit 0, the assertion line prints."""
    result = cli_runner.invoke(
        app,
        ["run", "code_pipeline", "--seed", "42", "--assert", "findings.race >= 1"],
    )
    assert result.exit_code == _exitcodes.OK, result.output
    assert "findings.race" in result.output
    assert "state_conflict" in result.output


def test_assert_findings_race_fails_the_run_when_the_threshold_is_not_met(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """A `--assert` that does not hold fails the run's exit code, same as a scenario assertion."""
    result = cli_runner.invoke(
        app,
        ["run", "code_pipeline", "--seed", "42", "--assert", "findings.race >= 99"],
    )
    assert result.exit_code == _exitcodes.ASSERTION_FAILURE, result.output


def test_assert_state_conflict_and_race_are_the_same_check(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """`findings.race`/`findings.state_conflict` must count identically — alias, not two checks."""
    race_result = cli_runner.invoke(
        app, ["run", "code_pipeline", "--seed", "42", "--assert", "findings.race >= 1"]
    )
    conflict_result = cli_runner.invoke(
        app, ["run", "code_pipeline", "--seed", "42", "--assert", "findings.state_conflict >= 1"]
    )
    assert race_result.exit_code == conflict_result.exit_code == _exitcodes.OK


def test_assert_findings_redundancy_sees_the_verdict_layers_own_finding(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """OP-2 first-pass finding #2 against `cli/` (`op2-audit-p17.md`), fixed.

    `code_pipeline` produces a real `redundancy` finding (`read_file` executed 2x
    concurrently, computed by `analysis/verdict.py`, not `analysis/race.py`). Before
    `cli._runsummary.combined_findings` existed, `CliRunSummary.findings` was built from
    `analysis.race_findings` alone, so this assertion always saw 0 matches regardless of the
    run's real output — demonstrated live in the audit against this exact run. It must now
    see the real count.
    """
    result = cli_runner.invoke(
        app, ["run", "code_pipeline", "--seed", "42", "--assert", "findings.redundancy >= 1"]
    )
    assert result.exit_code == _exitcodes.OK, result.output
    assert "findings.redundancy: 1 finding(s)" in result.output


def test_assert_findings_redundancy_does_not_vacuously_pass_an_upper_bound(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """The flip side of the same bug: `<= 0` must genuinely fail, not vacuously pass."""
    result = cli_runner.invoke(
        app, ["run", "code_pipeline", "--seed", "42", "--assert", "findings.redundancy <= 0"]
    )
    assert result.exit_code == _exitcodes.ASSERTION_FAILURE, result.output


def test_assert_malformed_expression_is_a_usage_error(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """A `--assert` that isn't 'PATH OP VALUE' fails fast, before any run starts."""
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--assert", "not a valid expr"])
    assert result.exit_code == _exitcodes.USAGE_ERROR
    assert "E-TARGET-009" in result.output


def test_assert_unsupported_path_is_a_usage_error(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """Only `findings.<type>` paths are supported.

    Anything else is a clear usage error, not a silent no-op or a fabricated pass.
    """
    result = cli_runner.invoke(
        app, ["run", "code_pipeline", "--assert", "speedup_vs_baseline >= 1"]
    )
    assert result.exit_code == _exitcodes.USAGE_ERROR
    assert "E-TARGET-009" in result.output


def test_assert_is_repeatable(cli_runner: CliRunner, isolated_data_dir: Path) -> None:
    """Multiple `--assert` flags all evaluate; a run passes only if every one does."""
    result = cli_runner.invoke(
        app,
        [
            "run",
            "code_pipeline",
            "--seed",
            "42",
            "--assert",
            "findings.race >= 1",
            "--assert",
            "findings.race <= 5",
        ],
    )
    assert result.exit_code == _exitcodes.OK, result.output
    assert result.output.count("✓") >= 2

    # One held, one didn't -> the run still fails overall (every --assert must hold).
    mixed = cli_runner.invoke(
        app,
        [
            "run",
            "code_pipeline",
            "--seed",
            "42",
            "--assert",
            "findings.race >= 1",
            "--assert",
            "findings.race >= 99",
        ],
    )
    assert mixed.exit_code == _exitcodes.ASSERTION_FAILURE, mixed.output
