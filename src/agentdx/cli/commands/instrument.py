"""`agentdx instrument` (PRD §37.1) — report what would be captured, without running anything.

Composes `scenario.validate.resolve_graph_identity` — the existing, tested, static (AST-only,
no import) discovery of a `graph.py`'s `add_node`/`@tool` literals — over a resolved `TARGET`.
This is a preview: it never imports or executes the target (the same "static, not an import"
guarantee `resolve_graph_identity`'s own module docstring describes for scenario validation),
so it can report before a single event is recorded. What it cannot tell a user is what a real
run would miss (`RunResult.gaps`, PRD §8.2) — that is inherently a runtime fact, surfaced by
`agentdx run`'s own output once a run exists, not by this static preview; declared here rather
than approximated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentdx.cli._exitcodes import OK, USAGE_ERROR
from agentdx.cli._output import Output
from agentdx.cli._target import find_repo_root, is_fixture_name
from agentdx.scenario.validate import resolve_graph_identity

__all__ = ["instrument"]


def instrument(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="A fixture name, or a graph import path (FILE.py:attribute).")
    ],
) -> None:
    """Report which nodes, tools and providers would be captured, and what would be missed."""
    out: Output = ctx.obj["output"]
    root = find_repo_root()
    fixture_name = is_fixture_name(target, root=root)
    target_field: dict[str, object] = (
        {"fixture": fixture_name} if fixture_name is not None else {"graph": target}
    )
    scenario_file = (root or Path.cwd()) / "agentdx-instrument-preview.yaml"
    identity = resolve_graph_identity(target_field, scenario_file=scenario_file)
    if identity is None:
        out.error(
            f"could not statically resolve {target!r} to a graph.py — "
            "is it a known fixture, or FILE.py:attribute pointing at a real file?"
        )
        raise typer.Exit(code=USAGE_ERROR)

    if out.json_mode:
        out.emit_json(
            {
                "source": str(identity.source),
                "agents": sorted(identity.agents),
                "tools": sorted(identity.tools),
                "edges": sorted(identity.edges),
            }
        )
    else:
        out.line(f"static discovery of {identity.source} (no import, no execution):")
        out.line(f"  agents: {', '.join(sorted(identity.agents)) or '(none found)'}")
        out.line(f"  tools:  {', '.join(sorted(identity.tools)) or '(none found)'}")
        out.line(f"  edges:  {', '.join(sorted(identity.edges)) or '(none found)'}")
        out.info(
            out.dim(
                "this is a static preview — what a run actually misses (state keys, "
                "provider calls) only shows up as `run_start.instrumentation_gap` events "
                "once `agentdx run` executes it"
            )
        )
    raise typer.Exit(code=OK)
