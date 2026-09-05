"""OP-2 second-pass finding #3 against `runtime/` (`op2-audit-p06-second.md`).

`runtime.scheduler.make_run_id` is only a 32-bit `blake2b` digest of
`(seed, scenario_hash, graph_hash)` — the audit demonstrated a real (not theoretical)
collision between two *different* input triples in under 30,000 tries. Before this repair,
`CliRunHost.open_run` (`cli/host.py`) could not tell a genuine D-80 rerun (identical inputs,
safe to reuse by I1, `RunAlreadyExistsError`) apart from an actual hash collision between two
different inputs — both hit the same "sealed row already exists" branch and both were
silently reused, printing the *wrong* run's stored verdict/exit code as if it were the answer
to whatever was actually asked to run (a real I9 risk).

Real accidental collisions are far too rare to hit by chance in a test (32-bit space), so
this monkeypatches `agentdx.cli.commands.run.make_run_id` to force one deterministically
between two invocations whose real, independently-resolved `(seed, scenario_hash,
graph_hash)` are provably different (different seeds). This exercises the real comparison
logic in `CliRunHost.open_run`, not a stand-in for it — only the 32-bit hash itself is faked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.main import app

_SCENARIO = """\
version: 1
scenario: {name}
description: minimal scenario for run_id collision test -- no faults, no success_check.

target:
  fixture: code_pipeline

task: fixtures/tasks/refactor_module.md
seed: {seed}

assertions:
  - max_findings: {{severity: critical, count: 1}}
"""

_RUN_ID_RE = re.compile(r"run_id:\s*(\S+)")


def _write_scenario(directory: Path, *, name: str, seed: int) -> Path:
    scenario_path = directory / f"{name}.yaml"
    scenario_path.write_text(_SCENARIO.format(name=name, seed=seed), encoding="utf-8")
    return scenario_path


def _run_id_from(output: str) -> str:
    match = _RUN_ID_RE.search(output)
    assert match is not None, f"no 'run_id: ...' line in output:\n{output}"
    return match.group(1)


def test_a_forced_run_id_collision_between_different_inputs_is_never_reused(
    cli_runner: CliRunner,
    isolated_data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine hash collision must surface as `INTERNAL_ERROR`, never a reused verdict.

    Runs scenario A (seed 42) to completion (sealed, real `run_id`). Then forces
    `agentdx.cli.commands.run.make_run_id` to return that *same* `run_id` for scenario B
    (seed 7) — reproducing the exact ambiguity finding #3 demonstrated against the real
    32-bit digest, deterministically. `open_run` must detect the seed mismatch and raise
    `RunIdCollisionError`, distinct from — and never caught as — `RunAlreadyExistsError`.
    """
    scenario_a = _write_scenario(tmp_path, name="collision_a", seed=42)
    first = cli_runner.invoke(app, ["run", str(scenario_a)])
    assert first.exit_code == _exitcodes.OK, first.output
    forced_run_id = _run_id_from(first.output)

    import agentdx.cli.commands.run as run_module

    monkeypatch.setattr(
        run_module,
        "make_run_id",
        lambda seed, scenario_hash, graph_hash: forced_run_id,
    )

    scenario_b = _write_scenario(tmp_path, name="collision_b", seed=7)
    second = cli_runner.invoke(app, ["run", str(scenario_b)])

    assert second.exit_code == _exitcodes.INTERNAL_ERROR, (second.output, second.stderr)
    assert "collision" in second.stderr.lower(), second.stderr
    assert "reused" not in second.output.lower()
    assert "reused" not in second.stderr.lower()

    # The original, unrelated run must remain exactly as it was -- untouched by the second
    # invocation's failure, not overwritten or reinterpreted.
    third = cli_runner.invoke(app, ["run", str(scenario_a)])
    assert third.exit_code == _exitcodes.OK, third.output
    assert "reused" in third.output.lower()
    assert _run_id_from(third.output) == forced_run_id
