"""D-80's `run_id`-collision handling: reused against a sealed run, replaced otherwise.

CONTEXT.md §9, ruled 2026-09-01, **C-34** §10, spec `d78-plan.md`. `run_id` is a pure
content hash of `seed`/`scenario_hash`/`graph_hash` (I1), so a collision is reused-and-
printed against a **sealed** prior run, and replaced against an **unsealed** one, rather
than the pre-D-80 `E-STORE-010` refusal every re-invocation used to hit.

`d78-plan.md` §7 names five decisive tests; this file is the CLI-level four of them
(1, 2, 3, 5) — test 4, "a sealed run is never deleted by the orphan path", is asserted at
the `store/` boundary in `tests/unit/store/test_discard_orphan_run.py`, closer to the
invariant it actually protects.

Every test here runs `code_pipeline` for real through the real `Scheduler`
(`d62-design.md` §8.7 — D-62 task #25's dispatch gap is what made this possible at all;
before that, no fixture reached `open_run` a second time to collide with anything).
`code_pipeline`'s own seeded G1 defect is exactly one real critical finding
(`fixtures/code_pipeline/golden_findings.json`, also cited by `scenarios/kill_reviewer.yaml`'s
own comment) — `max_findings: {severity: critical, count: N}` against that fixed, known
fact is what makes a scenario's pass/fail outcome deterministic without needing a fault or a
`success_check` at all.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app

_SCENARIO = """\
version: 1
scenario: d80_reuse_test
description: minimal scenario for D-80 reuse/replace tests -- no faults, no success_check.

target:
  fixture: code_pipeline

task: fixtures/tasks/refactor_module.md
seed: 42

assertions:
  - max_findings: {{severity: critical, count: {count}}}
"""

_RUN_ID_RE = re.compile(r"run_id:\s*(\S+)")


def _write_scenario(directory: Path, *, count: int) -> Path:
    """Write a scenario asserting `code_pipeline` has exactly `count` critical findings."""
    scenario_path = directory / "d80_reuse_test.yaml"
    scenario_path.write_text(_SCENARIO.format(count=count), encoding="utf-8")
    return scenario_path


def _run_id_from(output: str) -> str:
    match = _RUN_ID_RE.search(output)
    assert match is not None, f"no 'run_id: ...' line in output:\n{output}"
    return match.group(1)


def test_rerunning_a_sealed_passing_run_reuses_it_and_exits_0(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """`d78-plan.md` §7 test 1: reusing a sealed run prints findings and exits its own code."""
    scenario_path = _write_scenario(tmp_path, count=1)  # true count is 1: passes
    first = cli_runner.invoke(app, ["run", str(scenario_path)])
    assert first.exit_code == _exitcodes.OK, first.output

    second = cli_runner.invoke(app, ["run", str(scenario_path)])
    assert second.exit_code == _exitcodes.OK, second.output
    assert "reused" in second.output.lower()
    assert _run_id_from(first.output) == _run_id_from(second.output)


def test_rerunning_a_sealed_failing_run_reuses_it_and_exits_1_not_0(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """`d78-plan.md` §7 test 2 — the regression this change most needs to not introduce.

    A blanket exit 0 on reuse would turn a previously-red run green on re-invocation; exit
    codes are a MAJOR contract (PRD §37.2, `d78-plan.md` §4). `count: 0` is guaranteed false
    (the fixture's own real count is 1), so both invocations must fail identically.
    """
    scenario_path = _write_scenario(tmp_path, count=0)  # guaranteed false: real count is 1
    first = cli_runner.invoke(app, ["run", str(scenario_path)])
    assert first.exit_code == _exitcodes.ASSERTION_FAILURE, first.output

    second = cli_runner.invoke(app, ["run", str(scenario_path)])
    assert second.exit_code == _exitcodes.ASSERTION_FAILURE, second.output
    assert "reused" in second.output.lower()
    assert _run_id_from(first.output) == _run_id_from(second.output)


def test_an_orphaned_unsealed_run_is_replaced_and_a_real_run_proceeds(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """`d78-plan.md` §7 test 3: an orphan is replaced, not reused-as-nothing.

    Runs once to completion (sealed), then simulates the state an orphan is actually left
    in — `sealed_at = NULL`, the same shape D-62's own deadlock used to leave behind,
    reached here directly rather than by reproducing a scheduler deadlock, since `open_run`'s
    collision check only ever consults `sealed_at`, never how the run got that way. A second
    invocation must run for real (not print a stale "reused" result) and land on the exact
    same `run_id` as before, since the seed/scenario/graph are unchanged (I1).
    """
    scenario_path = _write_scenario(tmp_path, count=1)
    first = cli_runner.invoke(app, ["run", str(scenario_path)])
    assert first.exit_code == _exitcodes.OK, first.output
    run_id = _run_id_from(first.output)

    db_path = isolated_data_dir / "agentdx.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE runs SET sealed_at = NULL, status = 'running' WHERE run_id = ?", (run_id,)
        )
        conn.commit()
    finally:
        conn.close()

    second = cli_runner.invoke(app, ["run", str(scenario_path)])
    assert second.exit_code == _exitcodes.OK, second.output
    assert "reused" not in second.output.lower()
    assert _run_id_from(second.output) == run_id


def test_ci_json_marks_a_reused_run_but_not_a_fresh_one(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """`--ci` JSON must let a consumer tell a reuse apart from a fresh run.

    `d78-plan.md` §6's own open question, resolved additively via
    `cli.ci.ScenarioOutcome.reused`.
    """
    scenario_path = _write_scenario(tmp_path, count=1)
    out_dir = tmp_path / "ci-out"
    first = cli_runner.invoke(app, ["run", str(scenario_path), "--ci", "--out", str(out_dir)])
    assert first.exit_code == _exitcodes.OK, first.output
    first_json = (out_dir / "summary.json").read_text(encoding="utf-8")
    assert '"reused": true' not in first_json

    second = cli_runner.invoke(app, ["run", str(scenario_path), "--ci", "--out", str(out_dir)])
    assert second.exit_code == _exitcodes.OK, second.output
    second_json = (out_dir / "summary.json").read_text(encoding="utf-8")
    assert '"reused": true' in second_json
