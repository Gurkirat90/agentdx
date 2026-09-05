"""`agentdx compare` (PRD §37.1's two-run form; PRD §44.1's G6 gate, `RUN_ID --baseline`).

**Reconciling two texts for one command (a real, if small, PRD-internal gap — same class
`--assert`/`--faults`/`scenario run --repeat` already document, not hidden here either).**
PRD §37.1 gives `compare RUN_A RUN_B [--json] [--tolerance-file FILE] [--force]` — two
explicit run ids, diffing one sealed run against another. G6's own literal verification
command is `agentdx compare <run_id> --baseline` — one run id plus a flag. Both are real:
`RUN_B` here is an *optional* positional argument. Given only `RUN_A`, `--baseline` is
required and this generates (or would generate) a single-agent baseline for `RUN_A`'s own
target and diffs against that (PRD §17, the `analysis.baseline` pipeline, via `cli.
_baseline.CliBaselineExecutor`) — the same headline scorecard `analyze --scorecard` prints
for a single run, framed here as "compared to its baseline." Given both `RUN_A` and `RUN_B`,
this diffs two sealed runs directly (no baseline generation) — PRD §37.1's own form.

**What `--tolerance-file`/`--force` do today.** Declared in PRD §37.1 for the two-run form;
not wired to `cli.ci.check_regression`'s tolerance engine in this pass (that engine reads
`--ci`-written `CiSummary` JSON, a different data shape than two ids resolved from the
store) — passing either with the two-run form prints a warning and is otherwise a no-op,
the same honesty `run.py`'s own `--jobs`/`--fail-on` stubs already practice (never silently
pretending to check a tolerance nothing computed).

**Direct consequence, stated plainly (OP-2 first-pass finding #3 against `cli/`,
`op2-audit-p17.md`).** Because no regression logic exists in the two-run form regardless of
those flags, PRD §37.1's own documented exit contract for it ("0 no regression · 1 regression
beyond tolerance") cannot be reached — `_print_two_run_diff` always exits `OK`. `--baseline`
already has no pass/fail check of its own by design (see that function's own comment); the
two-run form's lack of one is a real, not-yet-closed gap against the PRD text, disclosed via
a runtime warning on every invocation rather than left implicit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentdx.analysis.baseline import BaselineAnalysisError, format_scorecard, generate_baseline
from agentdx.analysis.baseline import compare as compare_baseline
from agentdx.cli._analyze import analyze_events
from agentdx.cli._baseline import (
    CliBaselineExecutor,
    UnsupportedBaselineTargetError,
    baseline_available_for,
)
from agentdx.cli._config import GlobalOptions, resolve_config
from agentdx.cli._exitcodes import NOT_FOUND, OK, USAGE_ERROR
from agentdx.cli._output import Output
from agentdx.config import AgentDXConfig
from agentdx.events.schema import Event
from agentdx.store.sqlite import Store

__all__ = ["compare"]


def _task_text_for(target_name: str | None) -> str | None:
    """Return the task text a registered target's baseline graph was built for.

    `analysis.baseline.generate_baseline` needs the *identical* task text the multi-run
    executed (PRD §17.1 "same task", verbatim — see that function's own docstring); no event
    in this build's schema persists it (same gap `analysis.baseline`'s module docstring
    names), so it is read from the one place that does know it: the fixture module itself.
    """
    if target_name == "code_pipeline":
        from fixtures.code_pipeline.graph import TASK

        return TASK
    return None


def compare(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="The run to compare (or the left-hand run).")],
    run_id_b: Annotated[
        str | None,
        typer.Argument(
            help=(
                "A second run id to diff against directly (PRD §37.1). Omit and pass "
                "--baseline instead to compare RUN_ID against a generated single-agent "
                "baseline (PRD §44.1's G6 gate)."
            )
        ),
    ] = None,
    baseline: Annotated[
        bool,
        typer.Option(
            "--baseline",
            help="Generate a single-agent baseline for RUN_ID and compare against it (PRD §17).",
        ),
    ] = False,
    tolerance_file: Annotated[
        Path | None,
        typer.Option(
            "--tolerance-file",
            help=(
                "Per-metric regression tolerances for the two-run form. NOT YET WIRED — "
                "see module docstring."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Compare across a scenario-hash mismatch. NOT YET WIRED — see module docstring.",
        ),
    ] = False,
) -> None:
    """Compare a run against a generated baseline, or two sealed runs against each other."""
    out: Output = ctx.obj["output"]
    options: GlobalOptions = ctx.obj["options"]
    if run_id_b is None and not baseline:
        out.error("compare: pass a second RUN_ID, or --baseline to compare against a generated one")
        raise typer.Exit(code=USAGE_ERROR)
    if run_id_b is not None and baseline:
        out.error("compare: RUN_B and --baseline are mutually exclusive")
        raise typer.Exit(code=USAGE_ERROR)
    if tolerance_file is not None:
        out.warn("--tolerance-file: not yet wired for compare, ignored (see module docstring)")
    if force:
        out.warn("--force: not yet wired for compare, ignored (see module docstring)")

    try:
        config = resolve_config(options)
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc

    store_path = config.store.data_dir.expanduser() / "agentdx.db"
    store = Store.open(store_path, config=config.store)
    try:
        record_a = store.get_run(run_id)
        if record_a is None:
            out.error(f"no such run: {run_id!r}")
            raise typer.Exit(code=NOT_FOUND)
        events_a: tuple[Event, ...] = tuple(store.read_events(run_id))

        if run_id_b is not None:
            record_b = store.get_run(run_id_b)
            if record_b is None:
                out.error(f"no such run: {run_id_b!r}")
                raise typer.Exit(code=NOT_FOUND)
            events_b: tuple[Event, ...] = tuple(store.read_events(run_id_b))
            exit_code = _print_two_run_diff(out, run_id, events_a, run_id_b, events_b)
            raise typer.Exit(code=exit_code)

        exit_code = _print_baseline_diff(out, run_id, events_a, record_a.scenario_id, config=config)
        raise typer.Exit(code=exit_code)
    finally:
        store.close()


def _print_two_run_diff(
    out: Output,
    run_id_a: str,
    events_a: tuple[Event, ...],
    run_id_b: str,
    events_b: tuple[Event, ...],
) -> int:
    analysis_a = analyze_events(events_a)
    analysis_b = analyze_events(events_b)
    findings_a = len(analysis_a.race_findings) + len(analysis_a.verdict.findings)
    findings_b = len(analysis_b.race_findings) + len(analysis_b.verdict.findings)
    out.line(out.style(f"{run_id_a}  vs.  {run_id_b}", bold=True))
    out.line(
        f"  verdict       {analysis_a.verdict.verdict_class.value:<24} "
        f"{analysis_b.verdict.verdict_class.value}"
    )
    out.line(
        f"  score         {analysis_a.verdict.coordination_score!s:<24} "
        f"{analysis_b.verdict.coordination_score!s}"
    )
    out.line(f"  findings      {findings_a!s:<24} {findings_b!s}")
    out.line(
        f"  makespan_ms   {analysis_a.dag.virtual_makespan_ms!s:<24} "
        f"{analysis_b.dag.virtual_makespan_ms!s}"
    )
    # OP-2 first-pass finding #3 against `cli/` (`op2-audit-p17.md`): this form has no
    # regression/tolerance logic at all — PRD §37.1 documents "exit 1: regression beyond
    # tolerance" for `compare RUN_A RUN_B`, but nothing here ever computes one, so the exit
    # code can never be anything but OK. Disclosed rather than silently inherited from the
    # PRD's text, the same honesty `--tolerance-file`/`--force` already practice above.
    out.warn(
        "compare RUN_A RUN_B is informational only in this build — no regression tolerance "
        "is evaluated, so the exit code is always 0 regardless of the deltas above"
    )
    if out.json_mode:
        out.emit_json(
            {
                "run_id_a": run_id_a,
                "run_id_b": run_id_b,
                "verdict_class_a": analysis_a.verdict.verdict_class.value,
                "verdict_class_b": analysis_b.verdict.verdict_class.value,
                "coordination_score_a": analysis_a.verdict.coordination_score,
                "coordination_score_b": analysis_b.verdict.coordination_score,
                "findings_a": findings_a,
                "findings_b": findings_b,
                "makespan_ms_a": analysis_a.dag.virtual_makespan_ms,
                "makespan_ms_b": analysis_b.dag.virtual_makespan_ms,
                "regression_evaluated": False,
            }
        )
    out.coverage_statement()
    return OK


def _print_baseline_diff(
    out: Output,
    run_id: str,
    events: tuple[Event, ...],
    target_name: str | None,
    *,
    config: AgentDXConfig,
) -> int:
    if target_name is None or not baseline_available_for(target_name):
        out.error(
            f"run {run_id!r} (target {target_name!r}) has no registered baseline executor — "
            "only 'code_pipeline' today (see cli/_baseline.py)"
        )
        return USAGE_ERROR
    task = _task_text_for(target_name)
    if task is None:
        out.error(f"run {run_id!r}: no task text known for target {target_name!r}")
        return USAGE_ERROR

    executor = CliBaselineExecutor(target_name=target_name, config=config, out=out)
    try:
        baseline_run = generate_baseline(events, task, executor)
        comparison = compare_baseline(events, baseline_run)
    except UnsupportedBaselineTargetError as exc:
        out.error(str(exc))
        return USAGE_ERROR
    except BaselineAnalysisError as exc:
        out.error(str(exc))
        return USAGE_ERROR

    out.line(format_scorecard(comparison))
    if comparison.comparability.grade.value == "C":
        out.warn(f"comparability grade C: {comparison.comparability.reason}")
    if out.json_mode:
        out.emit_json(
            {
                "run_id": run_id,
                "target": target_name,
                "achieved_speedup": comparison.achieved_speedup,
                "ideal_parallel_speedup": comparison.ideal_parallel_speedup,
                "overhead_cost": comparison.overhead_cost,
                "gap": comparison.gap,
                "token_cost_multiplier": comparison.token_cost_multiplier,
                "comparability_grade": comparison.comparability.grade.value,
                "comparability_reason": comparison.comparability.reason,
            }
        )
    out.coverage_statement()
    # No pass/fail check is part of `--baseline`'s own spec (unlike `--assert` or a
    # scenario's tolerance check) — a low speedup or grade C is information this command
    # reports honestly, not a failure of the command itself. Exit OK once the comparison was
    # actually computed; USAGE_ERROR/NOT_FOUND above already cover the real failure paths.
    return OK
