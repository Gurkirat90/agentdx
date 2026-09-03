"""One test per PRD §37.2 exit code, each asserting the real process/CliRunner exit status.

**Why five of these monkeypatch `_execute_one` rather than running a real graph.** Every
fixture graph runs through `sdk/langgraph.py`'s real bindings against the real `Scheduler` —
`code_pipeline`'s own `planner -> {coder, reviewer} -> tester` fan-out completes end-to-end as
of D-62 task #25's candidate beta (`d62-design.md` §8.7, ADR pending as of 2026-09-03; see
that section for the two prior attempts this superseded and why). What still cannot be
produced by actually running a fixture is a cache-miss, an abort-guard trip, or a live
determinism leak — nothing in this repo's fixtures is built to hit those on purpose, so those
four exit codes are still substituted by monkeypatching `_execute_one`, exactly as before.
Everything downstream of `_execute_one` (exception classification, exit-code mapping,
`--json`/JUnit output) is exercised for real in every case; only the *cause* of the four
substituted failure modes is faked.

**History, for context.** This module's own claim used to be the opposite: that nothing in
`sdk/` ever called `Scheduler.spawn()` at all, so any real fan-out deadlocked the scheduler
(`E-SCHED-003`) — reliably reproduced as the exit-5 case below, at the time. That was already
stale once ADR-017 (D-62 Option B) wired `spawn()`/`join()` in; a second, narrower dispatch
bug (task #25's join() identity collision, then the dispatch gap itself) kept `code_pipeline`
deadlocking for a different reason until candidate beta closed it. `test_exit_5` below now
monkeypatches too, matching the other four, since a real scheduler deadlock is no longer
something any fixture in this repo reaches by actually running.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentdx.cli import _exitcodes
from agentdx.cli.commands import run as run_cmd
from agentdx.cli.main import app
from agentdx.runtime.determinism import DeterminismLeakError
from agentdx.runtime.faults.safety import AbortGuardTripped, GuardTrip
from agentdx.runtime.scheduler import DeadlockError
from agentdx.sdk.generic import CacheMissError


def test_exit_0_success_on_a_trivial_command(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """`version` never touches a run; it is the simplest real 0 (PRD §37.2)."""
    result = cli_runner.invoke(app, ["version"])
    assert result.exit_code == _exitcodes.OK


def test_exit_2_usage_error_on_unresolvable_target(
    cli_runner: CliRunner, isolated_data_dir: Path
) -> None:
    """A `TARGET` that names no fixture, scenario, or import path is a usage error."""
    result = cli_runner.invoke(app, ["run", "no_such_fixture_ever_exists"])
    assert result.exit_code == _exitcodes.USAGE_ERROR


def test_exit_5_internal_error_on_a_real_scheduler_deadlock(
    cli_runner: CliRunner,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler `DeadlockError` (`E-SCHED-003`) is exit 5 — see module docstring.

    No fixture in this repo reaches a real scheduler deadlock by actually running anymore
    (`code_pipeline` was the one that used to, until D-62 task #25's candidate beta) — same
    substitution as the other four exit codes below.
    """
    _monkeypatch_execute_one_to_raise(monkeypatch, DeadlockError({"t_r_fake_root_0": ""}))
    result = cli_runner.invoke(
        app,
        ["run", "code_pipeline", "--seed", "42", "--cache-mode", "replay"],
    )
    assert result.exit_code == _exitcodes.INTERNAL_ERROR


def test_exit_7_not_found_on_an_empty_scenario_directory(
    cli_runner: CliRunner, isolated_data_dir: Path, tmp_path: Path
) -> None:
    """A scenario-directory `TARGET` with zero `*.yaml`/`*.yml` files is exit 7, not 2."""
    empty_dir = tmp_path / "empty_scenarios"
    empty_dir.mkdir()
    result = cli_runner.invoke(app, ["run", str(empty_dir)])
    assert result.exit_code == _exitcodes.NOT_FOUND


def _monkeypatch_execute_one_to_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    async def _raise(**_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(run_cmd, "_execute_one", _raise)


def test_exit_3_cache_miss(
    cli_runner: CliRunner,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay-mode cache miss (`E-CACHE-001`) is exit 3 — see module docstring."""
    _monkeypatch_execute_one_to_raise(monkeypatch, CacheMissError("no cached response for key"))
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--seed", "1"])
    assert result.exit_code == _exitcodes.CACHE_MISS


def test_exit_4_guard_aborted(
    cli_runner: CliRunner,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abort-guard trip (`E-GUARD-001`) is exit 4 — see module docstring."""
    _monkeypatch_execute_one_to_raise(
        monkeypatch,
        AbortGuardTripped(GuardTrip(guard="max_virtual_duration_ms", detail="120000ms exceeded")),
    )
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--seed", "1"])
    assert result.exit_code == _exitcodes.GUARD_ABORTED


def test_exit_6_determinism_leak(
    cli_runner: CliRunner,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live determinism leak is exit 6.

    A documented judgement call — see `run.py`'s own `except DeterminismLeakError` comment
    and this response's NOT DONE/RISKS.
    """
    _monkeypatch_execute_one_to_raise(monkeypatch, DeterminismLeakError("time.time() called"))
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--seed", "1"])
    assert result.exit_code == _exitcodes.DETERMINISM_FAILURE


def test_exit_1_assertion_failure(
    cli_runner: CliRunner,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that completes but fails an assertion is exit 1, distinct from every exception path.

    Monkeypatches `_score_one` (not `_execute_one`) so the run "completes" and assertion
    evaluation runs for real against a fabricated failing result — same rationale as the
    other substituted cases, one level further down the pipeline.
    """
    from agentdx.scenario.assertions import AssertionResult, AssertionStatus

    async def _fake_execute_one(**kwargs: object) -> object:
        class _FakeHost:
            class _Opened:
                class _Ctx:
                    run_id = "r_fake01"

                context = _Ctx()
                fault_summary: tuple[object, ...] = ()

            opened = _Opened()

        class _FakeResult:
            output: dict[str, object]

            def __init__(self) -> None:
                self.output = {}

        return _FakeHost(), _FakeResult(), ()

    def _fake_score_one(**kwargs: object) -> tuple[object, object, tuple[AssertionResult, ...]]:
        failing = AssertionResult("task_success", AssertionStatus.FAILED, "forced failure")
        return None, None, (failing,)

    monkeypatch.setattr(run_cmd, "_execute_one", _fake_execute_one)
    monkeypatch.setattr(run_cmd, "_score_one", _fake_score_one)
    result = cli_runner.invoke(app, ["run", "code_pipeline", "--seed", "1"])
    assert result.exit_code == _exitcodes.ASSERTION_FAILURE
