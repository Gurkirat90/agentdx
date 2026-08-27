"""Typer application root: the `agentdx` console entry point (PRD §37).

Every command in PRD §37.1 is registered here so that `agentdx --help` prints the real command
list from day one and the surface cannot quietly drift from the spec. Commands with a real
implementation are registered from `cli.commands.*` (one module per command, `cli.commands`'
own docstring explains the split); everything still `_not_implemented` exits 2 (usage/
configuration error, PRD §37.2) naming the reason, rather than presenting a stub as working
behaviour (AGENTS.md §2). `ui` is P14's own command, unchanged by this prompt.

The `@app.callback()` below is new at P17: it parses PRD §37's global options once, before any
subcommand runs, and stores a `GlobalOptions` + `Output` pair on `ctx.obj` — every command
function takes `ctx: typer.Context` as its first parameter and reads `ctx.obj["options"]`/
`ctx.obj["output"]` rather than re-parsing `--json`/`--quiet`/... itself, so the PRD §37.3
output contract ("every command is scriptable") is enforced in one place.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from agentdx.cli._config import GlobalOptions
from agentdx.cli._output import Output
from agentdx.cli._target import find_repo_root
from agentdx.cli.commands import doctor as doctor_cmd
from agentdx.cli.commands import instrument as instrument_cmd
from agentdx.cli.commands import run as run_cmd
from agentdx.cli.commands import scenario as scenario_cmd
from agentdx.cli.commands import version as version_cmd


def _ensure_repo_root_on_path() -> None:
    """Put the checkout root on `sys.path`, mirroring `cli._target`'s own private helper.

    Every command that can name a fixture (`run`, `instrument`, and — the case that surfaced
    this — `scenario validate`'s own `success_check.ref: fixtures.<name>.checks:...` import)
    needs `fixtures.*` importable as a top-level package; `fixtures/` is not an installed
    distribution, only a checkout-relative directory, so nothing puts it on `sys.path` unless
    something does it explicitly. Idempotent and side-effect-free beyond the path insert.
    """
    root = find_repo_root()
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))


# Exit 2 = "usage, configuration or validation error" (PRD §37.2). The complete exit-code
# table lives in `cli._exitcodes`; only the one code this file's own stubs return is named
# here, so a not-yet-implemented command cannot drift from that table either.
_EXIT_NOT_IMPLEMENTED = 2

app = typer.Typer(
    name="agentdx",
    help=(
        "Multi-agent coordination debugger, deterministic replay runtime and chaos harness. "
        "Bounded search: absence of findings is not proof of absence."
    ),
    no_args_is_help=True,
    add_completion=False,
)

scenario_app = typer.Typer(name="scenario", help="Author, validate and expand scenario files.")
cache_app = typer.Typer(name="cache", help="Inspect and maintain the LLM record/replay cache.")
baseline_app = typer.Typer(name="baseline", help="Manage CI regression baselines.")
app.add_typer(scenario_app)
app.add_typer(cache_app)
app.add_typer(baseline_app)


def _not_implemented(command: str, prompt: str) -> NoReturn:
    """Exit 2 for a command that has no implementation yet, naming its owning prompt.

    Guarantees: writes a single diagnostic line to stderr and never writes to
    stdout, so `--json` consumers see an empty stdout rather than prose. Always
    raises; it never returns to the caller.
    """
    typer.echo(
        f"agentdx {command}: not implemented — owned by prompt {prompt}. "
        f"See CONTEXT.md §5 for build state, or this response's NOT DONE/RISKS.",
        err=True,
    )
    raise typer.Exit(code=_EXIT_NOT_IMPLEMENTED)


@app.callback()
def main_callback(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Override [store]/[run] data_dir.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Path to agentdx.toml (default: discovered).")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Print diagnostic detail.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress progress and informational lines.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable output on stdout only (PRD §37.3).")
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable ANSI colour.")] = False,
    seed: Annotated[int | None, typer.Option("--seed", help="Override [run] seed.")] = None,
    strict: Annotated[
        bool | None,
        typer.Option(
            "--strict/--no-strict",
            help="Override [scheduler] strict_determinism.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """AgentDX: instrument, run, replay and debug multi-agent coordination, deterministically."""
    options = GlobalOptions(
        data_dir=data_dir,
        config_path=config_path,
        verbose=verbose,
        quiet=quiet,
        json_mode=json_output,
        no_color=no_color,
        seed=seed,
        strict=strict,
    )
    output = Output(json_mode=json_output, quiet=quiet, verbose=verbose, no_color=no_color)
    ctx.obj = {"options": options, "output": output}
    _ensure_repo_root_on_path()


app.command(name="instrument")(instrument_cmd.instrument)
app.command(name="run")(run_cmd.run)
app.command(name="doctor")(doctor_cmd.doctor)
app.command(name="version")(version_cmd.version)
scenario_app.command(name="validate")(scenario_cmd.scenario_validate)
scenario_app.command(name="list")(scenario_cmd.scenario_list)
scenario_app.command(name="expand")(scenario_cmd.scenario_expand)


@app.command()
def replay() -> None:
    """Re-execute a recorded run from its log or bundle and verify canonical-log equality."""
    _not_implemented("replay", "P17 (deferred — see NOT DONE/RISKS)")


@app.command()
def analyze() -> None:
    """Re-run the analysers over a sealed log, producing a new analysis version."""
    _not_implemented("analyze", "P17 (deferred — see NOT DONE/RISKS)")


@app.command()
def compare() -> None:
    """Report metric deltas, findings added or removed, and verdict change between two runs."""
    _not_implemented("compare", "P17 (deferred — see NOT DONE/RISKS)")


@app.command(name="export")
def export_bundle() -> None:
    """Write a self-contained .agentdx bundle for a run."""
    _not_implemented("export", "P17 (deferred — see NOT DONE/RISKS)")


@app.command(name="import")
def import_bundle() -> None:
    """Load a .agentdx bundle, optionally verifying its hash chain."""
    _not_implemented("import", "P17 (deferred — see NOT DONE/RISKS)")


@app.command()
def ui(
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help=(
                "Bind address. Omit for 127.0.0.1 (loopback-only, PRD §26/§31.10's default "
                "posture). Passing --host explicitly is what Design Constraint 3 requires to "
                "bind anywhere else — doing so prints a no-auth warning, since this server "
                "has none."
            ),
        ),
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port. Omit for [api].port (default 8420).")
    ] = None,
) -> None:
    """Serve the API on 127.0.0.1:8420.

    P14 delivers the API layer only — no frontend was in this prompt's scope (see
    `docs/api.md`), so "the Control Tower" half of this command's own docstring-inherited
    promise is not yet built; this serves `agentdx.api.app`'s FastAPI app and nothing else.
    """
    from agentdx.api.app import serve

    try:
        serve(host=host, port=port, allow_non_local=host is not None)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=_EXIT_NOT_IMPLEMENTED) from exc


@app.command()
def bench() -> None:
    """Run a benchmark suite and write its results to bench/results/."""
    _not_implemented("bench", "P18")


@scenario_app.command(name="new")
def scenario_new() -> None:
    """Generate a scenario file, optionally derived from an existing run."""
    _not_implemented("scenario new", "P17 (deferred — see NOT DONE/RISKS)")


@cache_app.command(name="stats")
def cache_stats() -> None:
    """Report cache size, hit rate and per-run reuse."""
    _not_implemented("cache stats", "P17 (deferred — see NOT DONE/RISKS)")


@cache_app.command(name="verify")
def cache_verify() -> None:
    """Verify cache integrity against the recorded key hashes."""
    _not_implemented("cache verify", "P17 (deferred — see NOT DONE/RISKS)")


@cache_app.command(name="prune")
def cache_prune() -> None:
    """Remove cache entries no longer referenced by any retained run."""
    _not_implemented("cache prune", "P17 (deferred — see NOT DONE/RISKS)")


@cache_app.command(name="migrate")
def cache_migrate() -> None:
    """Migrate the cache to the current key-construction version."""
    _not_implemented("cache migrate", "P17 (deferred — see NOT DONE/RISKS)")


@cache_app.command(name="export")
def cache_export() -> None:
    """Export the cache entries referenced by a run."""
    _not_implemented("cache export", "P17 (deferred — see NOT DONE/RISKS)")


@baseline_app.command(name="update")
def baseline_update() -> None:
    """Refresh the committed CI regression baselines for a scenario directory."""
    _not_implemented("baseline update", "P17 (deferred — see NOT DONE/RISKS)")


def main() -> None:
    """Run the AgentDX command-line interface.

    Guarantees: this is the only console entry point; it delegates argument
    parsing to Typer and never catches an exception it cannot classify.
    """
    app()
