"""`agentdx analyze RUN_ID [--scorecard]` (PRD §44.1's G7 gate).

**Purpose split from `compare`.** `compare <run_id> --baseline` (`cli.commands.compare`)
frames a generated baseline as "this run vs. a comparison"; `analyze` frames the identical
underlying computation (`analysis.baseline.generate_baseline` + `compare`, via the same
`cli._baseline.CliBaselineExecutor`) as "the full analysis of this run" — re-running every
P10-P12 analyser (`cli._analyze.analyze_events`) over the sealed log and reporting the PRD
§18 verdict alongside the §17.4 scorecard `--scorecard` asks for. Both commands share the one
baseline-generation code path (`cli._baseline`) rather than duplicating it — AGENTS.md §2's
"never write a second copy of a rule."

**Why `--scorecard` is the flag that generates a baseline, not the default.** Every other
`analyze` output (verdict class, confidence, findings, recommendations) is computable from
the sealed log alone, with no baseline execution — cheap, and always available. The
scorecard's `achieved_speedup`/`ideal_parallel_speedup`/six-bucket attribution (PRD §17.3/
§17.4) specifically need a generated `BaselineRun`, which is real work (a second graph
execution, `cli._baseline.CliBaselineExecutor.execute`) — gated behind the flag that asks for
it, not paid on every `analyze` invocation.
"""

from __future__ import annotations

from typing import Annotated

import typer

from agentdx.analysis.baseline import (
    BaselineAnalysisError,
    format_scorecard,
    generate_baseline,
)
from agentdx.cli._analyze import analyze_events
from agentdx.cli._baseline import (
    CliBaselineExecutor,
    UnsupportedBaselineTargetError,
    baseline_available_for,
)
from agentdx.cli._config import GlobalOptions, resolve_config
from agentdx.cli._exitcodes import NOT_FOUND, OK, USAGE_ERROR
from agentdx.cli._output import Output
from agentdx.store.sqlite import Store

__all__ = ["analyze"]


def _task_text_for(target_name: str | None) -> str | None:
    """Return the task text a registered target's baseline graph was built for.

    Same reasoning and same limitation as `compare.py`'s identical helper: no event in this
    build's schema persists a run's task text (`analysis.baseline`'s own module docstring),
    so it is read from the one place that does — the fixture module itself.
    """
    if target_name == "code_pipeline":
        from fixtures.code_pipeline.graph import TASK

        return TASK
    return None


def analyze(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="The sealed run to re-analyse.")],
    scorecard: Annotated[
        bool,
        typer.Option(
            "--scorecard",
            help=(
                "Generate a single-agent baseline and print the PRD §17.4 speedup scorecard "
                "(achieved/ideal speedup, signed six-bucket attribution, comparability)."
            ),
        ),
    ] = False,
) -> None:
    """Re-run the analysers over a sealed log and print its verdict (and scorecard)."""
    out: Output = ctx.obj["output"]
    options: GlobalOptions = ctx.obj["options"]
    try:
        config = resolve_config(options)
    except Exception as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc

    store_path = config.store.data_dir.expanduser() / "agentdx.db"
    store = Store.open(store_path, config=config.store)
    try:
        record = store.get_run(run_id)
        if record is None:
            out.error(f"no such run: {run_id!r}")
            raise typer.Exit(code=NOT_FOUND)
        events = tuple(store.read_events(run_id))

        baseline_run = None
        if scorecard:
            target_name = record.scenario_id
            if target_name is None or not baseline_available_for(target_name):
                out.error(
                    f"run {run_id!r} (target {target_name!r}) has no registered baseline "
                    "executor — only 'code_pipeline' today (see cli/_baseline.py)"
                )
                raise typer.Exit(code=USAGE_ERROR)
            task = _task_text_for(target_name)
            if task is None:
                out.error(f"run {run_id!r}: no task text known for target {target_name!r}")
                raise typer.Exit(code=USAGE_ERROR)
            executor = CliBaselineExecutor(target_name=target_name, config=config, out=out)
            try:
                baseline_run = generate_baseline(events, task, executor)
            except UnsupportedBaselineTargetError as exc:
                out.error(str(exc))
                raise typer.Exit(code=USAGE_ERROR) from exc
            except BaselineAnalysisError as exc:
                out.error(str(exc))
                raise typer.Exit(code=USAGE_ERROR) from exc

        try:
            analysis = analyze_events(events, baseline=baseline_run)
        except BaselineAnalysisError as exc:
            out.error(str(exc))
            raise typer.Exit(code=USAGE_ERROR) from exc

        verdict = analysis.verdict
        out.line(
            out.style(
                f"{run_id}: {verdict.verdict_class.value} (confidence {verdict.confidence.value})",
                bold=True,
            )
        )
        if verdict.coordination_score is not None:
            out.line(f"  coordination score: {verdict.coordination_score}/100")
        for finding in verdict.findings:
            out.line(f"  [{finding.severity.value}] {finding.claim}")
        for rec in verdict.recommendations:
            out.line(f"  -> {rec.text}")

        if scorecard and analysis.comparison is not None:
            out.line("")
            out.line(format_scorecard(analysis.comparison))
            if analysis.comparison.comparability.grade.value == "C":
                out.warn(f"comparability grade C: {analysis.comparison.comparability.reason}")

        out.coverage_statement()
        raise typer.Exit(code=OK)
    finally:
        store.close()
