"""`agentdx run` (PRD §37.1, §22) — execute a target under the deterministic scheduler.

The composition root the mission's DELIVERABLES describe: resolves `TARGET` (`cli._target`),
builds the real runtime services and installs `cli.host.CliRunHost`, drives `agentdx.run()`
under the real `Scheduler`, then composes the analysis pipeline (`cli._analyze`) and assertion
evaluation (`scenario.assertions`, fed a `cli._runsummary.CliRunSummary`) over the sealed log.
One scenario file is one run; a scenario directory or `--ci` runs every scenario found in it
(PRD §22.1 item 5: sorted-path order) and, under `--ci`, writes the PRD §22.4 JSON/JUnit
artefacts (`cli.ci`) and PRD §22.6 regression comparison instead of prose.

**No new computation lives here.** Every number in a printed scorecard is read off an
`AnalysisResult`/`RunSummary` this module did not compute; every exit code is a direct
translation of an exception a lower module already raises for a reason that module already
owns (`CacheMissError` -> 3, `AbortGuardTripped` -> 4, `DeterminismLeakError` -> 6 — the last
a judgement call documented at its `except` clause, since PRD §37.2/§22.2's own text ties
exit 6 to *replay-verification* failure specifically and this run never performs one; see
this response's NOT DONE/RISKS).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import typer

import agentdx
from agentdx.cli import ci as ci_mod
from agentdx.cli._analyze import AnalysisResult, analyze_events
from agentdx.cli._config import GlobalOptions, resolve_config
from agentdx.cli._exitcodes import (
    ASSERTION_FAILURE,
    CACHE_MISS,
    DETERMINISM_FAILURE,
    GUARD_ABORTED,
    INTERNAL_ERROR,
    NOT_FOUND,
    OK,
    USAGE_ERROR,
)
from agentdx.cli._output import Output
from agentdx.cli._runsummary import CliRunSummary, combined_findings
from agentdx.cli._scenario_io import (
    LoadedScenario,
    ScenarioLoadError,
    ScenarioValidationError,
    discover_scenarios,
    load_and_validate,
)
from agentdx.cli._target import (
    TargetError,
    find_repo_root,
    is_fixture_name,
    resolve_target,
)
from agentdx.cli.host import (
    CliRunHost,
    RunAlreadyExistsError,
    build_cache,
    build_cache_hook,
    build_fault_hooks,
)
from agentdx.config import AgentDXConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.determinism import DeterminismLeakError
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.safety import AbortGuardTripped
from agentdx.runtime.scheduler import Scheduler, make_run_id
from agentdx.scenario.assertions import (
    AssertionResult,
    AssertionStatus,
    RunSummary,
    eval_findings_type_count,
    evaluate_assertion,
    is_known_finding_type,
    known_finding_type_spellings,
    load_success_check,
    run_python_success_check,
)
from agentdx.scenario.schema import Comparison, parse_comparison
from agentdx.sdk.generic import CacheMissError, RunResult, hash_text, install_runtime
from agentdx.store.sqlite import Store

__all__ = ["run"]


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """One scenario's (or one direct target's) fully evaluated outcome."""

    scenario_name: str
    run_id: str | None
    status: str
    exit_code: int
    assertions: tuple[AssertionResult, ...]
    analysis: AnalysisResult | None
    summary: CliRunSummary | None
    detail: str | None = None
    reused: bool = False
    """True iff this outcome is a stored, sealed run reused rather than freshly executed.

    D-80/C-34 (`d78-plan.md` §6's own open question, resolved here rather than left
    unimplemented): a reused outcome must be distinguishable from a fresh one, so a `--ci`
    consumer diffing two JSON payloads can tell "this re-ran" from "this was already
    known" — additive field only, `False` for every path that existed before D-80, so no
    existing consumer's parsing changes."""


def _is_scenario_path(target: str) -> bool:
    """Return whether `target` names a scenario file or a directory of scenario files.

    Guarantees: a directory that is a **known fixture** is never claimed here, so the caller
    falls through to `resolve_target`'s fixture branch. `is_fixture_name` is the single
    authority on what a fixture is (a `fixtures/<name>/graph.py` that exists) — this
    function does not re-derive that test, per CONTEXT.md §4's "never write a second copy of
    a rule" discipline.

    Why the fixture check is here and not only in `resolve_target`: this predicate decides
    which of two branches `_execute` takes, and it ran first. Because `path.is_dir()` alone
    claimed *any* existing directory, `agentdx run fixtures/code_pipeline` — PRD §38.1's own
    literal quickstart command — was routed to `discover_scenarios`, found no `*.yaml`, and
    exited 7 (`E-TARGET` was never consulted; `resolve_target` was never reached). That is
    the confirmed cause of gate G9's `exit 7, "no scenario files found under
    fixtures/code_pipeline"` and of the identical exit 7 from `docker-compose.yml`'s `seed`
    service, observed 2026-08-29 on real hardware. See D-77.
    """
    path = Path(target)
    if path.suffix in (".yaml", ".yml") and path.is_file():
        return True
    if not path.is_dir():
        return False
    return is_fixture_name(target) is None


def _resolve_task_text(task_field: str, *, scenario_dir: Path) -> str:
    """Return a scenario's task text: `task:` is a path when one resolves, else literal text.

    No existing module performs this resolution (`scenario/loader.py`'s own `resolve_defaults`
    leaves `task` exactly as written, per that field's own docstring on path-shaped inference)
    — every shipped scenario (`scenarios/kill_reviewer.yaml`) points `task:` at a committed
    `fixtures/tasks/*.md` file, so treating an existing-file value as "read this" rather than
    "use this string" is what makes the scenario's own task legible rather than the literal
    path being passed to `agentdx.run(task=...)`.
    """
    candidate = Path(task_field)
    if not candidate.is_absolute():
        candidate = scenario_dir / candidate
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8").strip()
    return task_field


def _build_fault_registry(
    faults: list[dict[str, object]], *, is_fixture_target: bool
) -> FaultRegistry | None:
    if not faults:
        return None
    resolved: dict[str, object] = {"faults": faults, "chaos_opt_in": is_fixture_target}
    return FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=is_fixture_target)


def _parse_fault_spec(spec: str) -> dict[str, object]:
    """Parse one `--faults TYPE:AGENT:AT_VIRTUAL_MS` ad-hoc spec (direct-target mode).

    A deliberately small shorthand — the mission gives no `--faults` grammar to implement
    (PRD §37.1's own text does not spell one out) and scenario files (PRD §12) already have a
    complete, richer `faults:` block; this exists only so `agentdx run <fixture> --faults ...`
    can arm a fault without hand-writing a scenario file. Declared, not hidden: `docs/cli.md`
    states this is CLI-invented syntax, not a PRD-given one.
    """
    parts = spec.split(":")
    if len(parts) < 3:
        msg = f"--faults spec {spec!r} must be TYPE:AGENT:AT_VIRTUAL_MS"
        raise TargetError("E-TARGET-008", msg)
    fault_type, agent, at_ms = parts[0], parts[1], parts[2]
    try:
        at_virtual_ts = int(at_ms)
    except ValueError as exc:
        msg = f"--faults spec {spec!r}: AT_VIRTUAL_MS must be an integer"
        raise TargetError("E-TARGET-008", msg) from exc
    return {
        "type": fault_type,
        "agent": agent,
        "at_virtual_ts": at_virtual_ts,
        "recoverable": False,
    }


def _parse_assert_expr(expr: str) -> tuple[str, Comparison]:
    """Parse one `--assert PATH OP VALUE` CLI expression (direct-target mode).

    A deliberately small, CLI-only mechanism — like `--faults`, PRD §37.1's own `agentdx run`
    flag list gives no grammar for `--assert` at all; its only textual source is PRD §44.1's
    G1 gate command, `agentdx run fixtures/code_pipeline --assert findings.race >= 1`. Declared
    here rather than hidden: `docs/cli.md` states this is CLI-invented syntax, same as
    `--faults`. Splits on whitespace into exactly three tokens (`PATH`, `OP`, `VALUE`) and reuses
    `scenario.schema.parse_comparison` for the `OP VALUE` half, so the accepted operators and
    number format are the single source of truth already governing every scenario YAML
    `cmp:`/comparison field, not a second, silently-divergent copy.

    Currently only `findings.<type-or-alias>` paths are supported (`eval_findings_type_count`
    in `scenario/assertions.py` does the actual evaluation) — no other path shape has a PRD
    citation or a test to validate against, so none is invented speculatively. The finding
    type itself is validated here too, against `scenario.assertions.is_known_finding_type` —
    before any run executes, so a typo is a `USAGE_ERROR` immediately rather than a silent
    vacuous PASS discovered only after the run finishes (OP-2 third-pass finding #1 against
    `scenario/`, `op2-audit-p08-third.md`: `--assert "findings.no_state_conflicts <= 0"` — a
    natural typo, confusing the finding-type name with this module's own built-in assertion
    name it resembles — used to report PASSED on a run with a real, seeded `state_conflict`
    finding, because a nonexistent type always counts zero matches, and `<= 0`/`>= 0` are true
    for zero regardless of whether the type is real).
    """
    parts = expr.split()
    if len(parts) != 3:
        msg = f"--assert {expr!r} must be 'PATH OP VALUE', e.g. 'findings.race >= 1'"
        raise TargetError("E-TARGET-009", msg)
    path, op, value = parts
    comparison = parse_comparison(f"{op} {value}")
    if comparison is None:
        msg = f"--assert {expr!r}: {op!r} {value!r} is not a valid comparison"
        raise TargetError("E-TARGET-009", msg)
    if not path.startswith("findings."):
        msg = (
            f"--assert {expr!r}: only 'findings.<type>' paths are supported currently, got {path!r}"
        )
        raise TargetError("E-TARGET-009", msg)
    finding_type = path.removeprefix("findings.")
    if not is_known_finding_type(finding_type):
        msg = (
            f"--assert {expr!r}: {finding_type!r} is not a finding type this build's analysis "
            f"layer produces — use one of {known_finding_type_spellings()!r}"
        )
        raise TargetError("E-TARGET-009", msg)
    return path, comparison


def _eval_assert_exprs(
    assert_exprs: list[tuple[str, Comparison]], run: RunSummary
) -> list[AssertionResult]:
    """Evaluate every parsed `--assert` expression against one run's `RunSummary`."""
    results = []
    for path, comparison in assert_exprs:
        finding_type = path.removeprefix("findings.")
        results.append(
            eval_findings_type_count(run, finding_type=finding_type, comparison=comparison)
        )
    return results


async def _execute_one(
    *,
    graph: object,
    task_text: str,
    scenario_id: str | None,
    scenario_hash: str,
    graph_hash: str,
    seed: int,
    config: AgentDXConfig,
    store: Store,
    faults: list[dict[str, object]],
    is_fixture_target: bool,
    cache_mode: str,
    run_mode: str,
    out: Output,
) -> tuple[CliRunHost, RunResult, tuple[Event, ...]]:
    """Run one instrumented graph to completion under a fresh `Scheduler`; return the host.

    Composition only (module docstring): builds the real `Scheduler`/`VirtualClock`/
    `EventWriter`/`Cache`/fault-hook services `cli.host.CliRunHost` needs, then drives
    `scheduler.run(agentdx.run(...))` — the ordering `host.py`'s own module docstring
    documents as the one place PRD/CONTEXT already committed to (see that module).
    """
    clock = VirtualClock()
    run_id = make_run_id(seed, scenario_hash, graph_hash)
    writer = EventWriter(run_id, store, wall_time_fn=agentdx.wall_time)
    registry = _build_fault_registry(faults, is_fixture_target=is_fixture_target)

    # `Scheduler(fault_hook=...)` only accepts a hook at construction time (no public setter
    # — `Scheduler`'s own docstring: "Do not call private methods from outside this class"),
    # but building the hook needs `scheduler.stamp` itself. `_scheduler_box` is the same
    # late-binding-closure trick `tests/integration/faults/_harness.py::build_scheduler`
    # already uses to break the cycle: the closure captures the box, not a `Scheduler`
    # instance, so it can be constructed before one exists and only reads it once called,
    # by which point `scheduler` below has been assigned into it.
    scheduler_box: list[Scheduler] = []

    def _stamp_via_scheduler(draft: DraftEvent, causes: Sequence[int]) -> Event:
        return scheduler_box[0].stamp(draft, causes)

    armed = build_fault_hooks(
        registry=registry, clock=clock, seed=seed, scheduler_stamp=_stamp_via_scheduler
    )
    if armed is not None and armed.unfireable:
        out.warn(
            "scenario arms fault type(s) "
            f"{', '.join(armed.unfireable)} with no live scheduler call site yet "
            "(host.py's own documented gap) — validated and recorded, never fired"
        )
    scheduler = Scheduler(
        run_id=run_id,
        seed=seed,
        clock=clock,
        writer=writer,
        config=config.scheduler,
        fault_hook=armed.hook if armed is not None else None,
    )
    scheduler_box.append(scheduler)
    cache = build_cache(
        cache_db_path=config.store.data_dir.expanduser() / config.cache.db_filename,
        mode=cache_mode,
        run_id=run_id,
    )
    host = CliRunHost(
        run_id=run_id,
        seed=seed,
        scheduler=scheduler,
        clock=clock,
        store=store,
        writer=writer,
        config=config,
        fault_registry=registry,
        scenario_id=scenario_id,
        scenario_hash=scenario_hash,
        graph_hash=graph_hash,
        cache_mode=cache_mode,
        run_mode=run_mode,
        cache=cache,
    )
    build_cache_hook(cache=cache, config=config)  # wired for a future scheduler cache_hook call
    previous_host = install_runtime(host)
    try:
        result = await scheduler.run(
            agentdx.run(graph, task=task_text, scenario=scenario_id, seed=seed)
        )
    finally:
        install_runtime(previous_host)
    events = tuple(store.read_events(run_id))
    return host, result, events


def _as_run_summary(summary: CliRunSummary) -> RunSummary:
    """Widen a `CliRunSummary` to the `scenario.assertions.RunSummary` Protocol it implements.

    `CliRunSummary`'s own docstring documents structural (not nominal) conformance —
    `AGENTS.md` §2's "no re-typing" precedent. mypy's Protocol-attribute matching is
    invariant on two counts a frozen dataclass cannot satisfy nominally even though it is
    behaviourally correct: (1) `findings: Sequence[Finding]` — `analysis.race.Finding`
    structurally satisfies `scenario.assertions.Finding` (both are exactly
    `type`/`severity`/`evidence_seq`, the same situation `cli._analyze`'s own documented
    cast already covers for the reverse direction); (2) a `Protocol`'s plain attribute
    annotations require a *settable* attribute by default (PEP 544), which a
    `frozen=True` dataclass field can never be — every field here is genuinely read-only
    by design (`AGENTS.md` §2: identity fields do not change after construction), not
    accidentally missing a setter. This cast documents both, once, at the one seam that
    needs it, rather than relaxing `CliRunSummary`'s own frozen contract to please mypy.
    """
    return cast("RunSummary", summary)


def _score_one(
    *,
    host: CliRunHost,
    run_result: RunResult,
    events: tuple[Event, ...],
    scenario: LoadedScenario | None,
    assert_exprs: list[tuple[str, Comparison]] = [],  # noqa: B006 - never mutated, only read
) -> tuple[AnalysisResult, CliRunSummary, tuple[AssertionResult, ...]]:
    analysis = analyze_events(events)
    opened = host.opened
    faults_fired = sum(1 for f in opened.fault_summary if getattr(f, "fired_count", 0))
    summary = CliRunSummary(
        run_id=opened.context.run_id,
        analysis=analysis,
        faults_fired=faults_fired,
        success_check_passed=None,
        deterministic_replay_verified=None,
        findings=combined_findings(analysis),
    )
    resolved = scenario.resolved if scenario is not None else None
    success_check = resolved.get("success_check") if resolved is not None else None
    if isinstance(success_check, dict) and success_check.get("type") == "python":
        ref = success_check.get("ref")
        final_state = run_result.output
        state_dict = final_state if isinstance(final_state, dict) else {"output": final_state}
        if isinstance(ref, str):
            fn = load_success_check(ref)
            check_result = run_python_success_check(fn, state_dict, _as_run_summary(summary))
            summary = CliRunSummary(
                run_id=summary.run_id,
                analysis=analysis,
                faults_fired=faults_fired,
                success_check_passed=check_result.status == AssertionStatus.PASSED,
                deterministic_replay_verified=None,
                findings=summary.findings,
            )
    assertion_items = resolved.get("assertions", []) if resolved is not None else []
    results: list[AssertionResult] = []
    if isinstance(assertion_items, list):
        for item in assertion_items:
            if isinstance(item, str | dict):
                results.append(evaluate_assertion(item, _as_run_summary(summary)))
    results.extend(_eval_assert_exprs(assert_exprs, _as_run_summary(summary)))
    return analysis, summary, tuple(results)


def _score_reused(
    *,
    run_id: str,
    events: tuple[Event, ...],
    scenario: LoadedScenario | None,
    assert_exprs: list[tuple[str, Comparison]] = [],  # noqa: B006 - never mutated, only read
) -> tuple[AnalysisResult, CliRunSummary, tuple[AssertionResult, ...]]:
    """Reconstruct a sealed run's own scorecard/findings/assertions from its stored log.

    D-80 (CONTEXT.md §9, ruled 2026-09-01, **C-34** §10): a `run_id` collision against a
    sealed run means the identical run already exists — I1 guarantees this stored log is
    what a fresh run would produce, so this recomputes exactly what `_score_one` would
    have, from the stored events, rather than executing anything a second time.

    **The one thing this cannot recompute, and does not pretend to:** a `python`-type
    `success_check` needs the graph's final output state (`run_result.output`), which is
    not part of the persisted event log — `RUN_END`'s own payload (`host.py::close_run`)
    carries counts and timings, never the graph's return value, by design. So
    `success_check_passed` stays `None` here. That is not a new special case:
    `evaluate_assertion` already treats `None` identically to "no `success_check`
    configured at all", the same value a scenario with none set produces on a fresh run —
    a check that reads `success_check_passed` reports `not_measurable`, not wrong or
    fabricated. `faults_fired` *is* fully recoverable, unlike the success-check output: it
    is a straight count of this run's own persisted `fault_injected` events, the same
    fact `close_run` would have reported live.
    """
    analysis = analyze_events(events)
    faults_fired = sum(1 for e in events if e.type is EventType.FAULT_INJECTED)
    summary = CliRunSummary(
        run_id=run_id,
        analysis=analysis,
        faults_fired=faults_fired,
        success_check_passed=None,
        deterministic_replay_verified=None,
        findings=combined_findings(analysis),
    )
    resolved = scenario.resolved if scenario is not None else None
    assertion_items = resolved.get("assertions", []) if resolved is not None else []
    results: list[AssertionResult] = []
    if isinstance(assertion_items, list):
        for item in assertion_items:
            if isinstance(item, str | dict):
                results.append(evaluate_assertion(item, _as_run_summary(summary)))
    results.extend(_eval_assert_exprs(assert_exprs, _as_run_summary(summary)))
    return analysis, summary, tuple(results)


def _print_human_outcome(out: Output, outcome: _RunOutcome) -> None:
    color = "green" if outcome.status == "passed" else "red"
    out.line(out.style(f"{outcome.scenario_name}: {outcome.status}", color=color, bold=True))
    if outcome.run_id is not None:
        out.line(f"  run_id: {outcome.run_id}")
    if outcome.reused:
        out.line("  (reused: identical seed/scenario/graph already ran; not re-executed — D-80)")
    if outcome.analysis is not None:
        verdict = outcome.analysis.verdict
        out.line(
            f"  verdict: {verdict.verdict_class.value} (confidence {verdict.confidence.value})"
        )
    for assertion in outcome.assertions:
        mark = {"passed": "✓", "failed": "✗", "not_measurable": "·"}[assertion.status]
        out.line(f"  {mark} {assertion.assertion_id}: {assertion.detail}")
    if outcome.detail:
        out.error(outcome.detail)
    out.coverage_statement()


def run(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "A fixture name, an import path (FILE.py:attribute), a scenario file, or a "
                "directory of scenario files."
            )
        ),
    ],
    task: Annotated[
        str | None,
        typer.Option(
            "--task", help="Task text, or a path to one. Overrides a scenario's own task."
        ),
    ] = None,
    seed: Annotated[
        int | None, typer.Option("--seed", help="Override the run seed (also a global option).")
    ] = None,
    faults: Annotated[
        list[str],
        typer.Option(
            "--faults",
            help="TYPE:AGENT:AT_VIRTUAL_MS — arm one fault ad hoc (direct-target mode only).",
        ),
    ] = [],  # noqa: B006 - Typer requires a literal default to derive the option's type
    cache_mode: Annotated[
        str | None,
        typer.Option(
            "--cache-mode", help="record|replay|perturb|passthrough (default: [run] mode)."
        ),
    ] = None,
    baseline: Annotated[
        bool,
        typer.Option(
            "--baseline",
            help=(
                "Generate a single-agent baseline for comparison. NOT YET IMPLEMENTED "
                "(needs analysis.baseline's BaselineExecutor, out of this prompt's scope) — "
                "passing this prints a warning and comparison-dependent assertions report "
                "not_measurable, exactly as they would with no baseline at all."
            ),
        ),
    ] = False,
    ci: Annotated[
        bool,
        typer.Option("--ci", help="Machine-readable mode (FR-11b): write JSON + JUnit, no prose."),
    ] = False,
    out_dir: Annotated[Path, typer.Option("--out", help="Directory for --ci artefacts.")] = Path(
        ".agentdx/ci"
    ),
    baseline_run: Annotated[
        Path | None,
        typer.Option(
            "--baseline-run", help="A --ci summary.json to regression-check against (§22.6)."
        ),
    ] = None,
    ci_format: Annotated[
        str,
        typer.Option(
            "--format",
            help=(
                "junit|json|github — which --ci artefact(s) to write. PRD §22.1's own flag; "
                "'github' is accepted but NOT YET IMPLEMENTED as a distinct GH-annotation "
                "renderer — falls back to junit+json, same as omitting --format."
            ),
        ),
    ] = "junit+json",
    jobs: Annotated[
        int,
        typer.Option(
            "--jobs",
            help=(
                "Parallelise --ci across N processes (PRD §22.1 item 5). NOT YET IMPLEMENTED — "
                "scenarios always run sequentially, in the same sorted-path order §22.1 "
                "requires either way; passing a value > 1 prints a warning and is otherwise "
                "a no-op."
            ),
        ),
    ] = 1,
    fail_on: Annotated[
        str | None,
        typer.Option(
            "--fail-on",
            help=(
                "Minimum finding severity that fails the run, independent of assertion "
                "results. NOT YET IMPLEMENTED — a scenario's own `max_findings` assertion is "
                "the only severity gate this build enforces; passing this prints a warning."
            ),
        ),
    ] = None,
    assert_exprs_raw: Annotated[
        list[str],
        typer.Option(
            "--assert",
            help=(
                "PATH OP VALUE, e.g. 'findings.race >= 1' — CLI-invented shorthand for a "
                "quick single-run assertion (PRD §44.1's G1 gate is its own only textual "
                "source; no PRD §37.1 grammar defines it, same class of addition as "
                "--faults). Repeatable; each one adds to the run's own assertion results "
                "and can fail the exit code exactly as a scenario file's assertions: block "
                "does. Currently only 'findings.<type-or-alias>' paths are supported."
            ),
        ),
    ] = [],  # noqa: B006 - Typer requires a literal default to derive the option's type
) -> None:
    """Execute a target under the deterministic scheduler and print the scorecard."""
    options: GlobalOptions = ctx.obj["options"]
    out_writer: Output = ctx.obj["output"]
    resolved_seed = seed if seed is not None else options.seed
    if jobs > 1:
        out_writer.warn(f"--jobs {jobs}: not yet implemented, running sequentially")
    if fail_on is not None:
        out_writer.warn(f"--fail-on {fail_on}: not yet implemented, ignored")
    if ci_format not in ("junit+json", "junit", "json", "github"):
        out_writer.error(f"--format must be junit|json|github, got {ci_format!r}")
        raise typer.Exit(code=USAGE_ERROR)
    try:
        assert_exprs = [_parse_assert_expr(expr) for expr in assert_exprs_raw]
    except TargetError as exc:
        out_writer.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    try:
        config = resolve_config(options)
    except Exception as exc:
        out_writer.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc

    try:
        exit_code = asyncio.run(
            _async_run(
                target=target,
                task=task,
                seed=resolved_seed,
                fault_specs=faults,
                cache_mode=cache_mode,
                baseline=baseline,
                ci=ci,
                ci_format=ci_format,
                out_dir=out_dir,
                baseline_run=baseline_run,
                config=config,
                out=out_writer,
                assert_exprs=assert_exprs,
            )
        )
    except TargetError as exc:
        out_writer.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    except (ScenarioLoadError, ScenarioValidationError) as exc:
        out_writer.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    raise typer.Exit(code=exit_code)


async def _async_run(
    *,
    target: str,
    task: str | None,
    seed: int | None,
    fault_specs: list[str],
    cache_mode: str | None,
    baseline: bool,
    ci: bool,
    ci_format: str,
    out_dir: Path,
    baseline_run: Path | None,
    config: AgentDXConfig,
    out: Output,
    assert_exprs: list[tuple[str, Comparison]] = [],  # noqa: B006 - never mutated, only read
) -> int:
    if baseline:
        out.warn(
            "--baseline is not yet implemented (needs analysis.baseline's BaselineExecutor) "
            "— comparison-dependent assertions will report not_measurable"
        )
    resolved_cache_mode = cache_mode if cache_mode is not None else config.run.mode
    store_path = config.store.data_dir.expanduser() / "agentdx.db"
    store = Store.open(store_path, config=config.store)
    outcomes: list[_RunOutcome] = []
    try:
        if _is_scenario_path(target):
            scenario_paths = discover_scenarios(Path(target))
            if not scenario_paths:
                out.error(f"no scenario files found under {target}")
                return NOT_FOUND
            for path in scenario_paths:
                outcomes.append(
                    await _run_scenario_file(
                        path,
                        seed_override=seed,
                        fault_specs=fault_specs,
                        cache_mode=resolved_cache_mode,
                        config=config,
                        store=store,
                        out=out,
                        assert_exprs=assert_exprs,
                    )
                )
        else:
            outcomes.append(
                await _run_direct_target(
                    target,
                    task=task,
                    seed=seed if seed is not None else config.run.seed,
                    fault_specs=fault_specs,
                    cache_mode=resolved_cache_mode,
                    config=config,
                    store=store,
                    out=out,
                    assert_exprs=assert_exprs,
                )
            )
    finally:
        store.close()

    return _finish(
        outcomes, ci=ci, ci_format=ci_format, out_dir=out_dir, baseline_run=baseline_run, out=out
    )


async def _run_direct_target(
    target: str,
    *,
    task: str | None,
    seed: int,
    fault_specs: list[str],
    cache_mode: str,
    config: AgentDXConfig,
    store: Store,
    out: Output,
    assert_exprs: list[tuple[str, Comparison]] = [],  # noqa: B006 - never mutated, only read
) -> _RunOutcome:
    graph_target = resolve_target(target, task=task)
    faults = [_parse_fault_spec(spec) for spec in fault_specs]
    scenario_hash = f"blake2b:{'0' * 64}"  # no scenario document in direct-target mode
    graph_hash = hash_text(graph_target.graph_hash_material)
    return await _run_and_score(
        graph=graph_target.graph,
        task_text=graph_target.task,
        scenario=None,
        scenario_name=graph_target.fixture_name or target,
        scenario_hash=scenario_hash,
        graph_hash=graph_hash,
        seed=seed,
        faults=faults,
        is_fixture_target=graph_target.is_fixture,
        cache_mode=cache_mode,
        config=config,
        store=store,
        out=out,
        assert_exprs=assert_exprs,
        direct_target_name=graph_target.fixture_name or target,
    )


async def _run_scenario_file(
    path: Path,
    *,
    seed_override: int | None,
    fault_specs: list[str],
    cache_mode: str,
    config: AgentDXConfig,
    store: Store,
    out: Output,
    assert_exprs: list[tuple[str, Comparison]] = [],  # noqa: B006 - never mutated, only read
) -> _RunOutcome:
    scenario = load_and_validate(path)
    resolved = scenario.resolved
    target_field = resolved.get("target")
    if not isinstance(target_field, dict):
        return _RunOutcome(
            scenario_name=scenario.scenario_id,
            run_id=None,
            status="failed",
            exit_code=USAGE_ERROR,
            assertions=(),
            analysis=None,
            summary=None,
            detail=f"{path}: no resolvable target",
        )
    fixture_name = target_field.get("fixture")
    graph_spec = target_field.get("graph")
    if isinstance(fixture_name, str):
        graph_target = resolve_target(fixture_name, task=None)
    elif isinstance(graph_spec, str):
        graph_target = resolve_target(graph_spec, task=str(resolved.get("task", "")))
    else:
        return _RunOutcome(
            scenario_name=scenario.scenario_id,
            run_id=None,
            status="failed",
            exit_code=USAGE_ERROR,
            assertions=(),
            analysis=None,
            summary=None,
            detail=f"{path}: target names neither a fixture nor a graph",
        )
    task_field = resolved.get("task")
    task_text = (
        _resolve_task_text(task_field, scenario_dir=find_repo_root() or path.parent)
        if isinstance(task_field, str)
        else graph_target.task
    )
    raw_faults = resolved.get("faults", [])
    faults = [f for f in raw_faults if isinstance(f, dict)] if isinstance(raw_faults, list) else []
    faults.extend(_parse_fault_spec(spec) for spec in fault_specs)
    resolved_seed_field = resolved.get("seed")
    scenario_seed = resolved_seed_field if isinstance(resolved_seed_field, int) else config.run.seed
    seed = seed_override if seed_override is not None else scenario_seed
    return await _run_and_score(
        graph=graph_target.graph,
        task_text=task_text,
        scenario=scenario,
        scenario_name=scenario.scenario_id,
        scenario_hash=scenario.scenario_hash,
        graph_hash=hash_text(graph_target.graph_hash_material),
        seed=seed,
        faults=faults,
        is_fixture_target=graph_target.is_fixture,
        cache_mode=cache_mode,
        config=config,
        store=store,
        out=out,
        assert_exprs=assert_exprs,
    )


async def _run_and_score(
    *,
    graph: object,
    task_text: str,
    scenario: LoadedScenario | None,
    scenario_name: str,
    scenario_hash: str,
    graph_hash: str,
    seed: int,
    faults: list[dict[str, object]],
    is_fixture_target: bool,
    cache_mode: str,
    config: AgentDXConfig,
    store: Store,
    out: Output,
    assert_exprs: list[tuple[str, Comparison]] = [],  # noqa: B006 - never mutated, only read
    direct_target_name: str | None = None,
) -> _RunOutcome:
    """Run one target and score it.

    `direct_target_name` populates `run_start.payload.scenario_id` (an existing, nullable
    field — never a schema change) for a direct-target run that has no scenario document of
    its own (`scenario is None`). Without this, a bare `agentdx run code_pipeline` leaves
    `scenario_id` permanently `None`, and nothing durable in the sealed log says which
    fixture a `run_id` came from — a real gap `compare --baseline`/`analyze --scorecard`
    (PRD §44.1 G6/G7) hit directly: both need to trace a stored `run_id` back to its target
    to know which `BaselineExecutor` graph to run. A scenario-file run already carries its
    own real `scenario.scenario_id` and takes precedence; this only fills the direct-target
    gap, so no existing consumer's `scenario_id` value changes.
    """
    try:
        host, run_result, events = await _execute_one(
            graph=graph,
            task_text=task_text,
            scenario_id=(scenario.scenario_id if scenario is not None else direct_target_name),
            scenario_hash=scenario_hash,
            graph_hash=graph_hash,
            seed=seed,
            config=config,
            store=store,
            faults=faults,
            is_fixture_target=is_fixture_target,
            cache_mode=cache_mode,
            run_mode="chaos" if faults else "baseline",
            out=out,
        )
    except RunAlreadyExistsError as exc:
        # D-80/C-34 (`d78-plan.md`): a sealed collision is reused and printed with *its own*
        # verdict's exit code, never a blanket 0 — re-scored from the stored log exactly as
        # `_score_one` would score a fresh execution (`_score_reused`'s own docstring), so a
        # previously-red run stays red on re-invocation (`d78-plan.md` §4's own governing
        # rule: exit codes are a MAJOR contract, PRD §37.2, and reuse must not flatten them).
        run_id = exc.record.run_id
        events = tuple(store.read_events(run_id))
        analysis, summary, assertions = _score_reused(
            run_id=run_id, events=events, scenario=scenario, assert_exprs=assert_exprs
        )
        failed = [a for a in assertions if a.status == AssertionStatus.FAILED]
        status = "failed" if failed else "passed"
        exit_code = ASSERTION_FAILURE if failed else OK
        return _RunOutcome(
            scenario_name=scenario_name,
            run_id=run_id,
            status=status,
            exit_code=exit_code,
            assertions=assertions,
            analysis=analysis,
            summary=summary,
            reused=True,
        )
    except CacheMissError as exc:
        return _RunOutcome(
            scenario_name, None, "failed", CACHE_MISS, (), None, None, detail=str(exc)
        )
    except AbortGuardTripped as exc:
        return _RunOutcome(
            scenario_name, None, "failed", GUARD_ABORTED, (), None, None, detail=str(exc)
        )
    except DeterminismLeakError as exc:
        # Judgement call, documented in this response's NOT DONE/RISKS: PRD §37.2/§22.2 tie
        # exit 6 to *replay-verification* failure specifically; this run performs none. A
        # live nondeterminism leak has no more specific code in the §37.2 table than this
        # one, and "internal error, never a user error" (exit 5's own docstring) is a worse
        # fit for a leak the *target's* code caused — exit 6 is the nearer of two imperfect
        # options.
        return _RunOutcome(
            scenario_name, None, "failed", DETERMINISM_FAILURE, (), None, None, detail=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - classified as internal error by design, see docstring
        return _RunOutcome(
            scenario_name, None, "failed", INTERNAL_ERROR, (), None, None, detail=repr(exc)
        )

    analysis, summary, assertions = _score_one(
        host=host,
        run_result=run_result,
        events=events,
        scenario=scenario,
        assert_exprs=assert_exprs,
    )
    failed = [a for a in assertions if a.status == AssertionStatus.FAILED]
    status = "failed" if failed else "passed"
    exit_code = ASSERTION_FAILURE if failed else OK
    return _RunOutcome(
        scenario_name=scenario_name,
        run_id=host.opened.context.run_id,
        status=status,
        exit_code=exit_code,
        assertions=assertions,
        analysis=analysis,
        summary=summary,
    )


def _finish(
    outcomes: list[_RunOutcome],
    *,
    ci: bool,
    ci_format: str,
    out_dir: Path,
    baseline_run: Path | None,
    out: Output,
) -> int:
    """Print/emit `outcomes` and return the process's overall exit code.

    Two independent output contracts, not one — `--ci` (PRD §22.4, writes `summary.json`/
    JUnit XML files under `--out`) and the global `--json` (PRD §37.3, one machine-readable
    object to stdout, human prose moves to stderr) can be set together or separately, and
    both are honoured when they are: `ci_mod.CiSummary` is the single JSON shape both draw
    from (OP-2 first-pass finding #1 against `cli/`, `op2-audit-p17.md` — before this fix,
    `--json` alone silently emitted nothing to stdout on this command, `--ci`'s own file
    output being a different contract entirely was no substitute for it). The human-prose
    loop still runs whenever `--ci` was not given, `--json` included — `Output.line()`
    already correctly routes it to stderr under `json_mode` (`_output.py`'s own contract);
    what was missing was ever writing the JSON object itself, not the prose routing.
    """
    worst = OK
    for outcome in outcomes:
        if outcome.exit_code != OK:
            worst = outcome.exit_code if worst == OK else worst

    if ci or out.json_mode:
        scenario_outcomes = tuple(
            ci_mod.ScenarioOutcome(
                scenario=o.scenario_name,
                run_id=o.run_id,
                status=o.status,
                assertions=_assertion_outcomes_of(o),
                verdict_class=(o.analysis.verdict.verdict_class.value if o.analysis else None),
                coordination_score=(o.analysis.verdict.coordination_score if o.analysis else None),
                metrics=_metrics_of(o),
                reused=o.reused,
            )
            for o in outcomes
        )
        summary = ci_mod.CiSummary(
            agentdx_version=agentdx.__version__,
            started_at="",
            duration_wall_s=0.0,
            scenarios=scenario_outcomes,
        )
        if ci:
            # `ci_format` values "github" and "junit+json" (the default) both write both
            # artefacts — "github" has no distinct renderer yet (`--format` option's own
            # help text says so); "junit"/"json" write only the one named.
            if ci_format in ("junit+json", "junit", "github"):
                ci_mod.write_junit_xml(summary, out_dir)
            if ci_format in ("junit+json", "json", "github"):
                ci_mod.write_json_summary(summary, out_dir)
            if baseline_run is not None and baseline_run.is_file():
                baseline_summary = ci_mod.load_summary(baseline_run)
                violations = ci_mod.check_regression(summary, baseline_summary)
                if violations:
                    for v in violations:
                        out.error(v.detail)
                    worst = ASSERTION_FAILURE
        if out.json_mode:
            out.emit_json(summary.as_dict())
        if ci:
            return worst

    for outcome in outcomes:
        _print_human_outcome(out, outcome)
    return worst


def _assertion_outcomes_of(outcome: _RunOutcome) -> tuple[ci_mod.AssertionOutcome, ...]:
    """Return the JUnit/JSON `assertions[]` for one outcome.

    A run that never reached assertion evaluation (`exit_code` set from a caught exception —
    `CacheMissError`/`AbortGuardTripped`/.../`DeadlockError`, `_run_and_score`'s own `except`
    ladder) has zero `AssertionResult`s, but a `<testsuite tests="0">` with no `<testcase>`
    at all silently drops the failure reason from the JUnit a CI consumer reads — so a run
    with a `detail` and no assertions gets one synthetic "run" testcase carrying it, instead
    of reporting nothing.
    """
    if outcome.assertions:
        return tuple(
            ci_mod.AssertionOutcome(
                name=a.assertion_id, status=a.status, expected=None, actual=a.detail
            )
            for a in outcome.assertions
        )
    if outcome.detail is not None:
        return (
            ci_mod.AssertionOutcome(
                name="run", status="failed", expected="run completes", actual=outcome.detail
            ),
        )
    return ()


def _metrics_of(outcome: _RunOutcome) -> dict[str, float]:
    if outcome.summary is None:
        return {}
    metrics: dict[str, float] = {}
    for name in (
        "speedup_vs_baseline",
        "resilience_score",
        "coordination_score",
        "token_cost_multiplier",
    ):
        value = outcome.summary.metric(name)
        if isinstance(value, (int, float)):
            metrics[name if name != "speedup_vs_baseline" else "achieved_speedup"] = float(value)
    return metrics
