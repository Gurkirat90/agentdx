"""Tests for `agentdx.scenario.assertions` — the pluggable hook and the nine built-ins.

`_FakeRunSummary`/`_FakeFinding` are plain dataclasses satisfying `assertions.RunSummary`/
`assertions.Finding` *structurally* (both are `@runtime_checkable Protocol`s) — exactly the
point of the module's design: nothing here imports a concrete type from `assertions.py`
beyond the Protocols themselves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentdx.scenario import assertions
from agentdx.scenario.schema import parse_comparison


@dataclass(frozen=True, slots=True)
class _FakeFinding:
    type: str
    severity: str
    evidence_seq: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _FakeRunSummary:
    run_id: str = "r_test"
    findings: tuple[_FakeFinding, ...] = ()
    faults_fired: int = 0
    success_check_passed: bool | None = None
    deterministic_replay_verified: bool | None = None
    metrics: dict[str, float | str] = field(default_factory=dict)

    def metric(self, name: str) -> float | str | None:
        return self.metrics.get(name)


def test_run_summary_and_finding_are_structurally_satisfied() -> None:
    run = _FakeRunSummary()
    assert isinstance(run, assertions.RunSummary)
    finding = _FakeFinding(type="state_conflict", severity="critical")
    assert isinstance(finding, assertions.Finding)


# ---------------------------------------------------------------------------------------
# no_state_conflicts / no_silent_failures / max_findings
# ---------------------------------------------------------------------------------------


def test_no_state_conflicts_passes_with_no_findings() -> None:
    result = assertions.eval_no_state_conflicts(_FakeRunSummary())
    assert result.status == assertions.AssertionStatus.PASSED


def test_no_state_conflicts_fails_on_a_critical_conflict() -> None:
    run = _FakeRunSummary(findings=(_FakeFinding("state_conflict", "critical", (13, 28)),))
    result = assertions.eval_no_state_conflicts(run)
    assert result.status == assertions.AssertionStatus.FAILED
    assert "13" in result.detail and "28" in result.detail


def test_no_state_conflicts_ignores_low_severity_conflicts() -> None:
    run = _FakeRunSummary(findings=(_FakeFinding("state_conflict", "low"),))
    result = assertions.eval_no_state_conflicts(run)
    assert result.status == assertions.AssertionStatus.PASSED


def test_no_silent_failures() -> None:
    ok = assertions.eval_no_silent_failures(_FakeRunSummary())
    assert ok.status == assertions.AssertionStatus.PASSED
    findings = (_FakeFinding("silent_failure", "high"),)
    bad = assertions.eval_no_silent_failures(_FakeRunSummary(findings=findings))
    assert bad.status == assertions.AssertionStatus.FAILED


def test_max_findings_boundary() -> None:
    run = _FakeRunSummary(findings=(_FakeFinding("x", "high"), _FakeFinding("x", "medium")))
    exactly_one_allowed = assertions.eval_max_findings(run, severity="high", count=1)
    assert exactly_one_allowed.status == assertions.AssertionStatus.PASSED
    zero_allowed = assertions.eval_max_findings(run, severity="high", count=0)
    assert zero_allowed.status == assertions.AssertionStatus.FAILED
    # "medium" findings count too, since medium >= medium in severity order
    medium_threshold = assertions.eval_max_findings(run, severity="medium", count=1)
    assert medium_threshold.status == assertions.AssertionStatus.FAILED  # 2 findings >= medium


# ---------------------------------------------------------------------------------------
# Metric-backed assertions — the "interface contract" degrade-to-not-measurable behaviour
# ---------------------------------------------------------------------------------------


def test_speedup_vs_baseline_not_measurable_when_metric_is_absent() -> None:
    cmp = parse_comparison(">= 1.0")
    result = assertions.eval_speedup_vs_baseline(_FakeRunSummary(), comparison=cmp)
    assert result.status == assertions.AssertionStatus.NOT_MEASURABLE


def test_speedup_vs_baseline_evaluates_when_metric_is_present() -> None:
    run = _FakeRunSummary(metrics={"speedup_vs_baseline": 1.4})
    passing = assertions.eval_speedup_vs_baseline(run, comparison=parse_comparison(">= 1.0"))
    assert passing.status == assertions.AssertionStatus.PASSED
    failing = assertions.eval_speedup_vs_baseline(run, comparison=parse_comparison(">= 2.0"))
    assert failing.status == assertions.AssertionStatus.FAILED


def test_resilience_score_and_token_cost_multiplier_read_their_own_metric_names() -> None:
    run = _FakeRunSummary(metrics={"resilience_score": 82.0, "token_cost_multiplier": 3.1})
    resilience = assertions.eval_resilience_score(run, comparison=parse_comparison(">= 70"))
    assert resilience.status == assertions.AssertionStatus.PASSED
    token_cost = assertions.eval_token_cost_multiplier(run, comparison=parse_comparison("<= 3.0"))
    assert token_cost.status == assertions.AssertionStatus.FAILED


def test_critical_path_share_reads_a_per_edge_metric_name() -> None:
    run = _FakeRunSummary(metrics={"critical_path_share:coder->reviewer": 0.61})
    result = assertions.eval_critical_path_share(
        run, edge="coder->reviewer", comparison=parse_comparison("<= 0.4")
    )
    assert result.status == assertions.AssertionStatus.FAILED
    assert "coder->reviewer" in result.detail


def test_task_success_and_deterministic_replay_not_measurable_by_default() -> None:
    run = _FakeRunSummary()
    assert assertions.eval_task_success(run).status == assertions.AssertionStatus.NOT_MEASURABLE
    replay = assertions.eval_deterministic_replay(run)
    assert replay.status == assertions.AssertionStatus.NOT_MEASURABLE


def test_task_success_and_deterministic_replay_evaluate_when_set() -> None:
    run = _FakeRunSummary(success_check_passed=True, deterministic_replay_verified=False)
    assert assertions.eval_task_success(run).status == assertions.AssertionStatus.PASSED
    assert assertions.eval_deterministic_replay(run).status == assertions.AssertionStatus.FAILED


# ---------------------------------------------------------------------------------------
# evaluate_assertion — dispatch over the bare/dict shapes validate.py already accepted
# ---------------------------------------------------------------------------------------


def test_evaluate_assertion_dispatches_bare_names() -> None:
    result = assertions.evaluate_assertion("no_state_conflicts", _FakeRunSummary())
    assert result.assertion_id == "no_state_conflicts"


def test_evaluate_assertion_dispatches_max_findings() -> None:
    run = _FakeRunSummary(findings=(_FakeFinding("x", "high"),))
    result = assertions.evaluate_assertion({"max_findings": {"severity": "high", "count": 0}}, run)
    assert result.status == assertions.AssertionStatus.FAILED


def test_evaluate_assertion_dispatches_critical_path_share() -> None:
    run = _FakeRunSummary(metrics={"critical_path_share:a->b": 0.1})
    item = {"critical_path_share": {"edge": "a->b", "cmp": "<= 0.4"}}
    result = assertions.evaluate_assertion(item, run)
    assert result.status == assertions.AssertionStatus.PASSED


def test_evaluate_assertion_rejects_an_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown assertion"):
        assertions.evaluate_assertion("not_a_real_assertion", _FakeRunSummary())


# ---------------------------------------------------------------------------------------
# The pluggable hook: loading and running a success_check
# ---------------------------------------------------------------------------------------


def test_load_success_check_imports_a_real_function() -> None:
    fn = assertions.load_success_check("os.path:isabs")
    assert callable(fn)


def test_load_success_check_raises_on_a_missing_function() -> None:
    with pytest.raises(assertions.SuccessCheckLoadError):
        assertions.load_success_check("os.path:this_does_not_exist")


def test_load_success_check_raises_without_a_colon() -> None:
    with pytest.raises(assertions.SuccessCheckLoadError):
        assertions.load_success_check("os.path.isabs")


def test_run_python_success_check_normalises_a_bare_bool() -> None:
    result = assertions.run_python_success_check(lambda s, r: True, {}, _FakeRunSummary())
    assert result.status == assertions.AssertionStatus.PASSED


def test_run_python_success_check_normalises_a_bool_detail_tuple() -> None:
    fn = lambda s, r: (False, "custom detail")  # noqa: E731 -- tiny fixture, no name needed
    result = assertions.run_python_success_check(fn, {}, _FakeRunSummary())
    assert result.status == assertions.AssertionStatus.FAILED
    assert result.detail == "custom detail"


# ---------------------------------------------------------------------------------------
# `run_python_success_check` wall-clock timeout (second independent OP-2 finding #5,
# 2026-08-16) — before this, only `run_shell_success_check` enforced PRD §21.6's "5 s wall"
# limit; a hung or looping `checks.py` function blocked indefinitely. Timeout is monkeypatched
# short here so the test suite doesn't pay the real configured timeout in wall-clock time.
# ---------------------------------------------------------------------------------------


def test_run_python_success_check_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assertions, "load_success_check_timeout_s", lambda: 1)

    def _slow(final_state: dict[str, object], run: object) -> bool:
        time.sleep(30)
        return True

    result = assertions.run_python_success_check(_slow, {}, _FakeRunSummary())
    assert result.status == assertions.AssertionStatus.FAILED
    assert "timed out" in result.detail


def test_run_python_success_check_does_not_leak_the_alarm_into_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out check must fully clear its `SIGALRM` handler and pending alarm.

    Otherwise a later, well-behaved check on the same (main) thread could be falsely killed
    by a stale alarm firing mid-execution.
    """
    monkeypatch.setattr(assertions, "load_success_check_timeout_s", lambda: 1)

    def _slow(final_state: dict[str, object], run: object) -> bool:
        time.sleep(30)
        return True

    timed_out = assertions.run_python_success_check(_slow, {}, _FakeRunSummary())
    assert timed_out.status == assertions.AssertionStatus.FAILED

    fast = assertions.run_python_success_check(lambda s, r: True, {}, _FakeRunSummary())
    assert fast.status == assertions.AssertionStatus.PASSED


def test_run_shell_success_check_exit_code_zero_passes() -> None:
    result = assertions.run_shell_success_check("exit 0")
    assert result.status == assertions.AssertionStatus.PASSED


def test_run_shell_success_check_nonzero_exit_fails() -> None:
    result = assertions.run_shell_success_check("exit 1")
    assert result.status == assertions.AssertionStatus.FAILED


def test_run_shell_success_check_times_out() -> None:
    result = assertions.run_shell_success_check("sleep 30", cwd=Path.cwd())
    assert result.status == assertions.AssertionStatus.FAILED


def test_run_shell_success_check_timeout_is_config_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #6 regression coverage.

    `_SHELL_TIMEOUT_S` was a bare literal; the timeout must now genuinely track
    `load_success_check_timeout_s()`, not just happen to equal it.
    """
    monkeypatch.setattr(assertions, "load_success_check_timeout_s", lambda: 1)
    result = assertions.run_shell_success_check("sleep 30", cwd=Path.cwd())
    assert result.status == assertions.AssertionStatus.FAILED
    assert "timed out after 1s" in result.detail
    assert "timed out" in result.detail
