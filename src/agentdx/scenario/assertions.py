"""The pluggable assertion hook (PRD §21.6) and the nine built-in assertions (PRD §21.7).

**Scope, stated plainly (this prompt's OUT OF SCOPE, and the mission's STOP CONDITIONS).**
Several built-in assertions — `speedup_vs_baseline`, `resilience_score`,
`critical_path_share`, `token_cost_multiplier` — read metrics that only exist once the
analysis layer computes them (scorecard, verdict), and that layer is P10/P11/P12, all
`NOT STARTED` (`CONTEXT.md` §5). The P08 prompt's own STOP CONDITIONS name this exact
situation ("§21.7's built-in assertions reference analysis fields not yet defined") as a
reason to stop and ask — and the same prompt's OUT OF SCOPE section pre-answers it: "no
assertion evaluation against real analysis output beyond the interface contract — the
analysers do not exist yet." This module takes that as the resolution rather than a fresh
question to re-litigate (flagged explicitly in the closing SELF-AUDIT, not silently acted
on): `RunSummary` below is a `Protocol` — the interface contract itself — and every built-in
assertion's evaluation logic is fully implemented and tested against hand-built fakes that
satisfy the Protocol. What does *not* exist is a producer of a real one; wiring a genuine
`RunSummary` from a real run is P10/P11's job, not this module's.

**Why a `Protocol`, not a dataclass owned here.** `fixtures/_harness.py` already ships a
`RunSummary` — its own docstring calls it "a fixture-local stand-in: PRD §21.6's example
signature names a RunSummary type that belongs to the not-yet-built api/analysis surface."
Scenario/ owning a second, competing concrete `RunSummary` dataclass would be exactly the
duplicated-source-of-truth pattern this codebase already treats as a defect (`config.py`'s
own precedent, `CONTEXT.md` D-12). A `Protocol` lets both the fixture-local stand-in *and*
whatever P10/P11 eventually builds satisfy this interface structurally, with scenario/
importing neither — the same escape hatch `analysis.baseline`'s `BaselineExecutor` Protocol
already uses to avoid importing `runtime` directly (invariant I3, `CONTEXT.md` §2).

**Design Constraint 5 — loading `checks.py` is a trust boundary.** `load_success_check`
imports fixture-local Python (exactly `validate.py`'s `success_check.ref` import, but at
evaluation time rather than load time — both call sites share this module's loader). This is
sanctioned for fixture-shipped code (PRD §13.3's sandboxed fixture set) and for a scenario's
own committed `checks.py`. **It must never be called with a `ref` resolved from a path inside
an extracted `.agentdx` bundle** — a bundle's `scenario.yaml` is data the bundle exporter
wrote, and a `success_check.ref` inside it pointing at a `checks.py` *also* inside that same
bundle would let an imported bundle execute arbitrary code on import, which is the exact
hazard `store/bundle.py`'s "member allowlist, no dynamic import" design (`CONTEXT.md` §13,
P03 session log) exists to prevent. `store/bundle.py` and `cli/` are the modules responsible
for keeping bundle-derived refs away from this function; this module only documents the
boundary, since it does not implement bundle import (out of scope).
"""

from __future__ import annotations

import importlib
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable

from agentdx.scenario.loader import load_success_check_timeout_s
from agentdx.scenario.schema import SEVERITY_ORDER, Comparison, Severity, parse_comparison


class _SuccessCheckTimeout(Exception):
    """Raised inside `run_python_success_check`'s `SIGALRM` handler; never escapes it."""


# ---------------------------------------------------------------------------------------
# The interface contract (see module docstring for why this is a Protocol, not a dataclass)
# ---------------------------------------------------------------------------------------


@runtime_checkable
class Finding(Protocol):
    """The shape of one finding.

    Matches the already-shipped `golden_findings.json` schema (`fixtures/*/golden_findings.json`,
    written and `VERIFIED` at P05 — this is not a guess).
    """

    type: str
    severity: str
    evidence_seq: Sequence[int]


@runtime_checkable
class RunSummary(Protocol):
    """What the built-in assertions (PRD §21.7) and a `checks.py` function (PRD §21.6) read.

    Guarantees this interface makes, and nothing more: `findings` and `success_check_passed`
    are concrete because their shape is already pinned by shipped code (`golden_findings.json`,
    `success_check`'s own pass/fail). `metric()` is deliberately a named, open lookup rather
    than a typed attribute per scorecard field — the scorecard/verdict shape (PRD §17.4) is
    prose and a mockup, not a schema, and pinning ten speculative field names here would create
    a false sense of a contract this module cannot actually enforce. A metric that is not
    (yet) computable returns `None`, and every assertion that reads one treats `None` as
    "not measurable", never as a failure — "Where the system cannot know something, it says
    so" (`AGENTS.md` §6).
    """

    run_id: str
    findings: Sequence[Finding]
    faults_fired: int
    success_check_passed: bool | None
    deterministic_replay_verified: bool | None

    def metric(self, name: str) -> float | str | None:
        """Return a named scorecard/verdict metric, or None if it is not yet computable.

        Recognised names, read by the built-in assertions below: `speedup_vs_baseline`,
        `resilience_score`, `token_cost_multiplier`, `comparability_grade` (str: "A"/"B"/"C"),
        `critical_path_share:<edge>` (one name per edge, e.g.
        `critical_path_share:coder->reviewer`).
        """
        ...


# ---------------------------------------------------------------------------------------
# Assertion results
# ---------------------------------------------------------------------------------------


class AssertionStatus:
    """The three outcomes an assertion evaluation can reach.

    Not an Enum: used as string literals in `AssertionResult.status` so a serialised result
    is directly the `assertion_result` event shape PRD §21.6 describes ("Result recorded as
    an `assertion_result` event"), without an extra enum-to-string translation step.
    """

    PASSED: Final = "passed"
    FAILED: Final = "failed"
    NOT_MEASURABLE: Final = "not_measurable"


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """The outcome of evaluating one assertion against one `RunSummary`.

    Guarantees: `status` is always one of `AssertionStatus`'s three values. `detail` is
    always populated — never an empty string — so an `assertion_result` event built from this
    (PRD §21.6: "part of the log, part of the evidence") is never evidence-empty in spirit,
    mirroring invariant I6's rule for findings.
    """

    assertion_id: str
    status: str
    detail: str


# ---------------------------------------------------------------------------------------
# Loading the pluggable hook (PRD §21.6)
# ---------------------------------------------------------------------------------------


class SuccessCheckLoadError(Exception):
    """Raised when a `success_check.ref` cannot be imported.

    Carries `E-SCEN-009` in spirit — `validate.py` raises the coded `ScenarioError` at load
    time using the same import machinery this exception wraps; this one is for a caller (e.g.
    a future run host) evaluating the check later, once validation has already passed.
    """


def load_success_check(
    ref: str,
) -> Callable[[dict[str, object], RunSummary], bool | tuple[bool, str]]:
    """Import and return the function named by a `type: python` `success_check.ref`.

    `ref` is `"<module.path>:<function_name>"` (PRD §21.6's own example:
    `"fixtures.code_pipeline.checks:module_a_compiles"`). See the module docstring's Design
    Constraint 5 note on where a `ref` may safely come from.

    Raises:
        SuccessCheckLoadError: the module or function does not exist, or is not callable.
    """
    if ":" not in ref:
        detail = f"success_check.ref {ref!r} must be '<module.path>:<function_name>'"
        raise SuccessCheckLoadError(detail)
    module_path, _, func_name = ref.rpartition(":")
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, func_name)
    except (ImportError, AttributeError) as exc:
        detail = f"success_check.ref {ref!r} is not importable: {exc}"
        raise SuccessCheckLoadError(detail) from exc
    if not callable(fn):
        detail = f"success_check.ref {ref!r} resolved to a non-callable"
        raise SuccessCheckLoadError(detail)
    return cast("Callable[[dict[str, object], RunSummary], bool | tuple[bool, str]]", fn)


def run_python_success_check(
    fn: Callable[[dict[str, object], RunSummary], bool | tuple[bool, str]],
    final_state: dict[str, object],
    run: RunSummary,
) -> AssertionResult:
    """Run a loaded `type: python` success check, normalising its `bool | (bool, str)` return.

    Enforces the same PRD §21.6 wall-clock limit `run_shell_success_check` already did via
    `subprocess.run(timeout=...)` — OP-3 repair (2026-08-16, second independent OP-2 finding
    #5): before this, only the shell variant had a timeout at all, so a hung or looping
    `checks.py` function blocked indefinitely. Implemented with `signal.alarm`, the simplest
    mechanism that can interrupt an arbitrary in-process Python call without threads or
    subprocesses — a declared limitation, not a hidden one: `SIGALRM` only exists on Unix and
    only fires in the main thread (this project's CI targets `ubuntu-latest`/`macos-14` only,
    per P01; there is no Windows target). When called from a non-main thread or where
    `SIGALRM` is unavailable, `fn` runs unbounded, same as before this repair — `can_use_alarm`
    below is `False` in exactly that case, and no exception is raised for it.
    """
    timeout_s = load_success_check_timeout_s()
    on_main_thread = threading.current_thread() is threading.main_thread()
    can_use_alarm = hasattr(signal, "SIGALRM") and on_main_thread

    def _on_alarm(signum: int, frame: object) -> None:
        raise _SuccessCheckTimeout

    previous_handler = None
    if can_use_alarm:
        previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(timeout_s)
    try:
        outcome = fn(final_state, run)
    except _SuccessCheckTimeout:
        return AssertionResult(
            "task_success",
            AssertionStatus.FAILED,
            f"timed out after {timeout_s}s: {fn.__qualname__}",
        )
    finally:
        if can_use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
    passed, detail = outcome if isinstance(outcome, tuple) else (outcome, None)
    status = AssertionStatus.PASSED if passed else AssertionStatus.FAILED
    return AssertionResult(
        "task_success", status, detail or f"{fn.__qualname__} returned {passed!r}"
    )


def run_shell_success_check(command: str, *, cwd: Path | None = None) -> AssertionResult:
    """Run a `type: shell` success check. Exit code 0 = success (PRD §21.6)."""
    timeout_s = load_success_check_timeout_s()
    try:
        # This *is* the sanctioned `type: shell` success-check path (PRD §21.6): `command`
        # is scenario-declared, run in the sandbox, under a wall-clock timeout. Not a
        # generic exec helper; no other call site in this module uses `shell=True`.
        completed = subprocess.run(  # noqa: S602 -- scenario-declared command, sanctioned by PRD §21.6
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AssertionResult(
            "task_success",
            AssertionStatus.FAILED,
            f"timed out after {timeout_s}s: {command}",
        )
    status = AssertionStatus.PASSED if completed.returncode == 0 else AssertionStatus.FAILED
    tail = (completed.stdout or b"").decode("utf-8", errors="replace")[-500:]
    return AssertionResult("task_success", status, f"exit {completed.returncode}: {tail}")


# ---------------------------------------------------------------------------------------
# Built-in assertions (PRD §21.7)
# ---------------------------------------------------------------------------------------


def _not_measurable(assertion_id: str, reason: str) -> AssertionResult:
    return AssertionResult(assertion_id, AssertionStatus.NOT_MEASURABLE, reason)


def _severity_at_least(finding: Finding, minimum: Severity) -> bool:
    try:
        actual = Severity(finding.severity)
    except ValueError:
        return False
    return SEVERITY_ORDER[actual] >= SEVERITY_ORDER[minimum]


def eval_no_state_conflicts(run: RunSummary) -> AssertionResult:
    """Pass iff zero `state_conflict` findings at `high`/`critical` (PRD §21.7)."""
    offending = [
        f
        for f in run.findings
        if f.type == "state_conflict" and _severity_at_least(f, Severity.HIGH)
    ]
    if offending:
        seqs = sorted({s for f in offending for s in f.evidence_seq})
        return AssertionResult(
            "no_state_conflicts",
            AssertionStatus.FAILED,
            f"{len(offending)} conflict(s), evidence seq {seqs}",
        )
    return AssertionResult(
        "no_state_conflicts", AssertionStatus.PASSED, "no high/critical state_conflict findings"
    )


def eval_no_silent_failures(run: RunSummary) -> AssertionResult:
    """Pass iff no finding is classified `silent_failure` (PRD §21.7, §12.2 byzantine/malformed)."""
    offending = [f for f in run.findings if f.type == "silent_failure"]
    if offending:
        seqs = sorted({s for f in offending for s in f.evidence_seq})
        return AssertionResult(
            "no_silent_failures",
            AssertionStatus.FAILED,
            f"{len(offending)} silent_failure finding(s), evidence seq {seqs}",
        )
    return AssertionResult(
        "no_silent_failures", AssertionStatus.PASSED, "no silent_failure findings"
    )


def eval_max_findings(run: RunSummary, *, severity: str, count: int) -> AssertionResult:
    """Pass iff findings at or above `severity` number `<= count` (PRD §21.7)."""
    minimum = Severity(severity)
    offending = [f for f in run.findings if _severity_at_least(f, minimum)]
    assertion_id = f"max_findings[{severity}]"
    if len(offending) > count:
        return AssertionResult(
            assertion_id,
            AssertionStatus.FAILED,
            f"{len(offending)} finding(s) at >= {severity}, allowed {count}",
        )
    return AssertionResult(
        assertion_id,
        AssertionStatus.PASSED,
        f"{len(offending)} finding(s) at >= {severity}, allowed {count}",
    )


def _eval_metric_comparison(
    run: RunSummary, *, metric_name: str, assertion_id: str, comparison: Comparison
) -> AssertionResult:
    value = run.metric(metric_name)
    if value is None:
        return _not_measurable(
            assertion_id, f"metric '{metric_name}' is not yet computable for this run"
        )
    if not isinstance(value, int | float):
        return _not_measurable(
            assertion_id, f"metric '{metric_name}' returned a non-numeric value {value!r}"
        )
    passed = comparison.evaluate(float(value))
    status = AssertionStatus.PASSED if passed else AssertionStatus.FAILED
    return AssertionResult(assertion_id, status, f"{metric_name}={value} vs. {comparison}")


def eval_speedup_vs_baseline(run: RunSummary, *, comparison: Comparison) -> AssertionResult:
    """Pass iff achieved speedup satisfies the comparison (PRD §21.7; requires comparability >= B).

    The comparability gate: a grade-C result is "excluded from CI assertions by default" (PRD
    §17.5) unless the scenario set `baseline.allow_low_comparability: true` — that flag is a
    load-time (`validate.py`) concern about whether this assertion is even *declared*
    validly, not an evaluate-time one, so it is not re-checked here; `metric()` itself is free
    to fold the exclusion into what it returns.
    """
    return _eval_metric_comparison(
        run,
        metric_name="speedup_vs_baseline",
        assertion_id="speedup_vs_baseline",
        comparison=comparison,
    )


def eval_resilience_score(run: RunSummary, *, comparison: Comparison) -> AssertionResult:
    """Pass iff the aggregate resilience score satisfies the comparison (PRD §21.7)."""
    return _eval_metric_comparison(
        run, metric_name="resilience_score", assertion_id="resilience_score", comparison=comparison
    )


def eval_token_cost_multiplier(run: RunSummary, *, comparison: Comparison) -> AssertionResult:
    """Pass iff the token cost multiplier satisfies the comparison (PRD §21.7)."""
    return _eval_metric_comparison(
        run,
        metric_name="token_cost_multiplier",
        assertion_id="token_cost_multiplier",
        comparison=comparison,
    )


def eval_critical_path_share(
    run: RunSummary, *, edge: str, comparison: Comparison
) -> AssertionResult:
    """Pass iff the named edge's critical-path share satisfies the comparison (PRD §21.7)."""
    return _eval_metric_comparison(
        run,
        metric_name=f"critical_path_share:{edge}",
        assertion_id=f"critical_path_share[{edge}]",
        comparison=comparison,
    )


def eval_task_success(run: RunSummary) -> AssertionResult:
    """Pass iff the configured `success_check` passed (PRD §21.7)."""
    if run.success_check_passed is None:
        return _not_measurable("task_success", "no success_check ran for this run")
    status = AssertionStatus.PASSED if run.success_check_passed else AssertionStatus.FAILED
    return AssertionResult(
        "task_success", status, f"success_check_passed={run.success_check_passed}"
    )


def eval_deterministic_replay(run: RunSummary) -> AssertionResult:
    """Pass iff a verification replay matched the canonical hash (PRD §21.7, invariant I1)."""
    if run.deterministic_replay_verified is None:
        return _not_measurable(
            "deterministic_replay", "no verification replay was run for this run"
        )
    status = AssertionStatus.PASSED if run.deterministic_replay_verified else AssertionStatus.FAILED
    return AssertionResult(
        "deterministic_replay",
        status,
        f"deterministic_replay_verified={run.deterministic_replay_verified}",
    )


def evaluate_assertion(item: str | dict[str, object], run: RunSummary) -> AssertionResult:
    """Evaluate one already-`validate()`-passed assertion entry (bare name or `{name: params}`).

    This is the runtime half of what `validate._check_assertions` checks structurally at load
    time — the two are deliberately not merged into one pass: this function assumes its input
    already passed validation (an unknown name or malformed params raises `ValueError` here,
    on the theory that reaching this function with invalid input is a caller bug, not a user
    error to render nicely).
    """
    if isinstance(item, str):
        name, params = item, None
    else:
        name, params = next(iter(item.items()))

    match name:
        case "no_state_conflicts":
            return eval_no_state_conflicts(run)
        case "no_silent_failures":
            return eval_no_silent_failures(run)
        case "task_success":
            return eval_task_success(run)
        case "deterministic_replay":
            return eval_deterministic_replay(run)
        case "max_findings":
            if not isinstance(params, dict):
                detail = f"max_findings needs {{severity, count}} params, got {params!r}"
                raise TypeError(detail)
            return eval_max_findings(run, severity=params["severity"], count=params["count"])
        case "critical_path_share":
            if not isinstance(params, dict):
                detail = f"critical_path_share needs {{edge, cmp}} params, got {params!r}"
                raise TypeError(detail)
            comparison = parse_comparison(params["cmp"])
            if comparison is None:
                detail = f"invalid critical_path_share.cmp: {params['cmp']!r}"
                raise ValueError(detail)
            return eval_critical_path_share(run, edge=params["edge"], comparison=comparison)
        case "speedup_vs_baseline" | "resilience_score" | "token_cost_multiplier":
            if not isinstance(params, str):
                detail = f"{name} needs a comparison string, got {params!r}"
                raise TypeError(detail)
            comparison = parse_comparison(params)
            if comparison is None:
                detail = f"invalid comparison for {name}: {params!r}"
                raise ValueError(detail)
            evaluator = {
                "speedup_vs_baseline": eval_speedup_vs_baseline,
                "resilience_score": eval_resilience_score,
                "token_cost_multiplier": eval_token_cost_multiplier,
            }[name]
            return evaluator(run, comparison=comparison)
        case _:
            detail = f"unknown assertion {name!r}"
            raise ValueError(detail)


__all__ = [
    "AssertionResult",
    "AssertionStatus",
    "Finding",
    "RunSummary",
    "SuccessCheckLoadError",
    "eval_critical_path_share",
    "eval_deterministic_replay",
    "eval_max_findings",
    "eval_no_silent_failures",
    "eval_no_state_conflicts",
    "eval_resilience_score",
    "eval_speedup_vs_baseline",
    "eval_task_success",
    "eval_token_cost_multiplier",
    "evaluate_assertion",
    "load_success_check",
    "run_python_success_check",
    "run_shell_success_check",
]
