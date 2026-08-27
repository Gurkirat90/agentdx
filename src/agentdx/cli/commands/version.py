"""`agentdx version` (PRD §37.1) — print the package and event-schema versions."""

from __future__ import annotations

import typer

import agentdx
from agentdx.cli._exitcodes import OK
from agentdx.cli._output import Output
from agentdx.events.schema import SCHEMA_VERSION

__all__ = ["version"]


def version(ctx: typer.Context) -> None:
    """Print the AgentDX version and the event schema version."""
    out: Output = ctx.obj["output"]
    if out.json_mode:
        out.emit_json({"agentdx_version": agentdx.__version__, "schema_version": SCHEMA_VERSION})
    else:
        out.line(f"agentdx {agentdx.__version__} (event schema v{SCHEMA_VERSION})")
    raise typer.Exit(code=OK)
