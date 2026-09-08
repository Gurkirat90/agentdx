"""D-88 (CONTEXT.md §9, ruled 2026-09-07, addendum 2026-09-08) — `CliRunHost`-level carve-out.

`op2-audit-p13.md` finding #2 demonstrated live that `explore()` sweeping many distinct
`delay_schedule`s at one fixed seed would, if wired to `CliRunHost` with its default D-80 reuse
behavior, silently print the *first* schedule's stored result for the second and later ones —
`run_id` has no `delay_schedule` component, so every schedule in a sweep collides on the same
sealed row. `RunAlreadyExistsError`'s reuse guarantee (I1: identical inputs -> byte-identical
output) does not hold across different schedules at one seed, which is exactly what a sweep is.

`CliRunHost(..., bypass_run_id_reuse=True)` turns that silent-fabrication failure mode into a
loud one: the same sealed-match case that would otherwise raise `RunAlreadyExistsError` (and get
reused) instead raises `RunIdReuseDisabledError`. No shipped CLI/`explore()` call site sets this
flag yet (`explore()`'s own `ExecuteFn` protocol has no `CliRunHost` dependency at all today) —
this test exercises the primitive directly, the same way a library function is tested ahead of
its first caller.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.host import CliRunHost, RunAlreadyExistsError, RunIdReuseDisabledError
from agentdx.cli.main import app
from agentdx.store.sqlite import Store

_SCENARIO = """\
version: 1
scenario: {name}
description: minimal scenario for the D-88 run_id-reuse-bypass test -- no faults, no success_check.

target:
  fixture: code_pipeline

task: fixtures/tasks/refactor_module.md
seed: {seed}

assertions:
  - max_findings: {{severity: critical, count: 1}}
"""

_RUN_ID_RE = re.compile(r"run_id:\s*(\S+)")


def _run_id_from(output: str) -> str:
    match = _RUN_ID_RE.search(output)
    assert match is not None, f"no 'run_id: ...' line in output:\n{output}"
    return match.group(1)


def test_bypass_flag_defaults_false_and_ordinary_reruns_are_unaffected(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """D-80's own reuse behavior (`RunAlreadyExistsError`) must be completely unchanged for
    every caller that doesn't opt in -- `agentdx run`'s own two-invocation rerun still reuses
    and prints, exactly as before this row's fix.
    """
    scenario = tmp_path / "rerun.yaml"
    scenario.write_text(_SCENARIO.format(name="d88_rerun", seed=99), encoding="utf-8")

    first = cli_runner.invoke(app, ["run", str(scenario)])
    assert first.exit_code == _exitcodes.OK, first.output

    second = cli_runner.invoke(app, ["run", str(scenario)])
    assert second.exit_code == _exitcodes.OK, second.output
    assert "reused" in second.output.lower()
    assert _run_id_from(first.output) == _run_id_from(second.output)


def test_bypass_true_raises_reuse_disabled_instead_of_silently_reusing(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """The exact hazard `op2-audit-p13.md` finding #2 demonstrated, closed at the primitive
    level: a second `CliRunHost` built with `bypass_run_id_reuse=True` against the *same*
    already-sealed `run_id` must raise `RunIdReuseDisabledError`, not reuse it -- and the two
    exception types must be genuinely distinct, so a caller catching one never silently catches
    the other.
    """
    scenario = tmp_path / "seed.yaml"
    scenario.write_text(_SCENARIO.format(name="d88_seed", seed=7), encoding="utf-8")

    sealed = cli_runner.invoke(app, ["run", str(scenario)])
    assert sealed.exit_code == _exitcodes.OK, sealed.output
    run_id = _run_id_from(sealed.output)

    store = Store.open(isolated_data_dir / "agentdx.db")
    record = store.get_run(run_id)
    assert record is not None
    assert record.sealed

    # Every other CliRunHost constructor argument is untouched before open_run() raises on the
    # sealed-match branch (checked directly against host.py's own open_run(), which reads
    # self._store first) -- None stand-ins are safe here, not a shortcut around real behavior.
    host = CliRunHost(
        run_id=run_id,
        seed=record.seed,
        scheduler=None,  # type: ignore[arg-type]  # never read before the raise
        clock=None,  # type: ignore[arg-type]
        store=store,
        writer=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        fault_registry=None,
        scenario_id=record.scenario_id,
        scenario_hash=record.scenario_hash,
        graph_hash=record.graph_hash,
        cache_mode="replay",
        run_mode=record.mode,
        bypass_run_id_reuse=True,
    )

    with pytest.raises(RunIdReuseDisabledError) as excinfo:
        import asyncio

        asyncio.run(host.open_run(task="refactor", scenario=None, seed=record.seed))

    assert excinfo.value.record.run_id == run_id
    assert not isinstance(excinfo.value, RunAlreadyExistsError)

    # And the original sealed run is completely untouched by the attempt.
    still_there = store.get_run(run_id)
    assert still_there is not None
    assert still_there.sealed
