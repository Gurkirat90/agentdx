"""`agentdx scenario validate|list|expand` (PRD §37.1) — the three read-only scenario commands.

Grouped in one module because all three share the same load chain (`cli._scenario_io`) and
none of them executes anything — `scenario new` (generating a scenario, optionally from a run)
needs a run to derive from and is deferred; see this response's NOT DONE/RISKS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentdx.cli._exitcodes import NOT_FOUND, OK, USAGE_ERROR
from agentdx.cli._output import Output
from agentdx.cli._scenario_io import (
    ScenarioLoadError,
    ScenarioValidationError,
    discover_scenarios,
    load_and_validate,
)
from agentdx.scenario.matrix import MatrixError, expand_matrix

__all__ = ["scenario_expand", "scenario_list", "scenario_validate"]


def scenario_validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A scenario file or a directory of them.")],
) -> None:
    """Validate a scenario file against the schema and the chaos safety rules."""
    out: Output = ctx.obj["output"]
    paths = discover_scenarios(path)
    if not paths:
        out.error(f"no scenario files found at {path}")
        raise typer.Exit(code=NOT_FOUND)
    failures = 0
    results = []
    for one in paths:
        try:
            loaded = load_and_validate(one)
        except (ScenarioLoadError, ScenarioValidationError) as exc:
            failures += 1
            out.error(f"{one}: {exc}")
            results.append({"path": str(one), "ok": False, "detail": str(exc)})
        else:
            out.line(out.style(f"✓ {one} ({loaded.scenario_id})", color="green"))
            results.append({"path": str(one), "ok": True, "scenario_id": loaded.scenario_id})
    if out.json_mode:
        out.emit_json({"results": results, "ok": failures == 0})
    raise typer.Exit(code=OK if failures == 0 else USAGE_ERROR)


def scenario_list(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(help="A directory of scenario files (default: cwd).")
    ] = Path(),
) -> None:
    """List the scenarios discoverable from the current directory."""
    out: Output = ctx.obj["output"]
    paths = discover_scenarios(path)
    if not paths:
        out.error(f"no scenario files found under {path}")
        raise typer.Exit(code=NOT_FOUND)
    rows: list[dict[str, object]] = []
    for one in paths:
        try:
            loaded = load_and_validate(one)
        except (ScenarioLoadError, ScenarioValidationError) as exc:
            rows.append({"path": str(one), "scenario_id": None, "error": str(exc)})
            out.warn(f"{one}: {exc}")
        else:
            rows.append({"path": str(one), "scenario_id": loaded.scenario_id})
            out.line(f"{loaded.scenario_id}\t{one}")
    if out.json_mode:
        out.emit_json({"scenarios": rows})
    raise typer.Exit(code=OK)


def scenario_expand(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A scenario file with a `matrix:` block.")],
) -> None:
    """Print the full matrix expansion of a scenario file."""
    out: Output = ctx.obj["output"]
    try:
        loaded = load_and_validate(path)
    except (ScenarioLoadError, ScenarioValidationError) as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    try:
        expansion = expand_matrix(loaded.resolved)
    except MatrixError as exc:
        out.error(str(exc))
        raise typer.Exit(code=USAGE_ERROR) from exc
    if not expansion:
        out.info(f"{path} has no `matrix:` block — a single scenario, {loaded.scenario_id!r}")
        if out.json_mode:
            out.emit_json({"members": []})
        raise typer.Exit(code=OK)
    if out.json_mode:
        out.emit_json(
            {
                "members": [
                    {"scenario_id": m.scenario_id, "assignment": dict(m.assignment)}
                    for m in expansion
                ]
            }
        )
    else:
        for member in expansion:
            assignment = ", ".join(f"{k}={v!r}" for k, v in member.assignment)
            out.line(f"{member.scenario_id}: {assignment}")
    raise typer.Exit(code=OK)
