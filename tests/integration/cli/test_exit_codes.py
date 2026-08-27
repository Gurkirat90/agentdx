"""One test per PRD §37.2 exit code, each asserting the real process/CliRunner exit status.

**Why four of these monkeypatch `_execute_one` rather than running a real graph.** Building
this CLI surfaced a gap this prompt's DELIVERABLES do not own: nothing in `sdk/` — neither
`sdk.langgraph.LangGraphAdapter` nor `sdk.generic`'s `@agent`/`@tool` decorator path — ever
calls `runtime.scheduler.Scheduler.spawn()`. Every real fixture graph therefore executes as
one plain coroutine inside the scheduler's single root task; the moment LangGraph's own
executor suspends on anything that is not `scheduler.yield_point()`/`scheduler.sleep()` (its
parallel-branch fan-out, in `fixtures/code_pipeline`), the scheduler sees no runnable task and
no pending timer and raises `DeadlockError` (`E-SCHED-003`) — reliably reproduced below as the
exit-5 case. This is a real, previously-undiscovered gap (host.py's own module docstring
already flagged the *risk* of fixtures meeting a real `Scheduler` for the first time; this is
the specific failure that risk predicted) — see this response's NOT DONE/RISKS. It is a `sdk/`
defect, not a `cli/` one: everything downstream of `_execute_one` (exception classification,
exit-code mapping, `--json`/JUnit output) is exercised for real; only the *cause* of a
cache-miss/guard-trip/determinism-leak is substituted, because no fixture can currently reach
one of those specific failure modes by actually running.
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
    cli_runner: CliRunner, isolated_data_dir: Path, repo_root: Path
) -> None:
    """A genuinely completed CLI invocation: real Scheduler, real fixture, real deadlock.

    See module docstring — `code_pipeline`'s parallel `coder`/`reviewer` branch is not
    reachable by the scheduler's cooperative loop because `sdk/` never spawns it as a task.
    """
    result = cli_runner.invoke(
        app,
        ["run", "code_pipeline", "--seed", "42", "--cache-mode", "replay"],
        catch_exceptions=False,
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
