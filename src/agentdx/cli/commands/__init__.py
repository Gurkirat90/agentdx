"""One Typer command implementation per module (PRD §37.1, mission DELIVERABLES).

Each module exports a plain function carrying its own `typer.Option`/`typer.Argument`
annotations; `cli.main` registers it against the shared `app`/`scenario_app`/... instances
with `app.command(name=...)(module.func)` rather than decorating in place here, so this
package has no import-time dependency on `cli.main` (avoiding a cycle) and every command's
`--help` text lives beside its implementation.
"""

from __future__ import annotations
