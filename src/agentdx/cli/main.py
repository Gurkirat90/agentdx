"""Typer application root: the `agentdx` console entry point (PRD §37).

Every command in PRD §37.1 is registered here so that `agentdx --help` prints the
real command list from day one and the surface cannot quietly drift from the spec.
No command has an implementation yet — each exits 2 (usage/configuration error,
PRD §37.2) with an explicit "not implemented" message naming the prompt that owns
it. These are declared-empty commands, not stubs presented as working behaviour
(AGENTS.md §2). The full command bodies, the authoritative exit-code table and the
output conventions land at P17.
"""

from typing import NoReturn

import typer

# Exit 2 = "usage, configuration or validation error" (PRD §37.2). The complete
# exit-code enum is owned by P17; only the one code this file can legitimately
# return is named here, so that P17 is not pre-empted.
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
        f"See CONTEXT.md §5 for build state.",
        err=True,
    )
    raise typer.Exit(code=_EXIT_NOT_IMPLEMENTED)


@app.command()
def instrument() -> None:
    """Report which nodes, tools and providers would be captured, and what would be missed."""
    _not_implemented("instrument", "P04")


@app.command()
def run() -> None:
    """Execute a target under the deterministic scheduler and print the scorecard."""
    _not_implemented("run", "P17")


@app.command()
def replay() -> None:
    """Re-execute a recorded run from its log or bundle and verify canonical-log equality."""
    _not_implemented("replay", "P17")


@app.command()
def analyze() -> None:
    """Re-run the analysers over a sealed log, producing a new analysis version."""
    _not_implemented("analyze", "P17")


@app.command()
def compare() -> None:
    """Report metric deltas, findings added or removed, and verdict change between two runs."""
    _not_implemented("compare", "P17")


@app.command(name="export")
def export_bundle() -> None:
    """Write a self-contained .agentdx bundle for a run."""
    _not_implemented("export", "P03")


@app.command(name="import")
def import_bundle() -> None:
    """Load a .agentdx bundle, optionally verifying its hash chain."""
    _not_implemented("import", "P03")


@app.command()
def doctor() -> None:
    """Check the environment for anything that would silently break determinism."""
    _not_implemented("doctor", "P19")


@app.command()
def ui() -> None:
    """Serve the Control Tower and the API on 127.0.0.1:8420."""
    _not_implemented("ui", "P14")


@app.command()
def bench() -> None:
    """Run a benchmark suite and write its results to bench/results/."""
    _not_implemented("bench", "P18")


@app.command()
def version() -> None:
    """Print the AgentDX version and the event schema version."""
    _not_implemented("version", "P19")


@scenario_app.command(name="validate")
def scenario_validate() -> None:
    """Validate a scenario file against the schema and the chaos safety rules."""
    _not_implemented("scenario validate", "P08")


@scenario_app.command(name="list")
def scenario_list() -> None:
    """List the scenarios discoverable from the current directory."""
    _not_implemented("scenario list", "P08")


@scenario_app.command(name="new")
def scenario_new() -> None:
    """Generate a scenario file, optionally derived from an existing run."""
    _not_implemented("scenario new", "P08")


@scenario_app.command(name="expand")
def scenario_expand() -> None:
    """Print the full matrix expansion of a scenario file."""
    _not_implemented("scenario expand", "P08")


@cache_app.command(name="stats")
def cache_stats() -> None:
    """Report cache size, hit rate and per-run reuse."""
    _not_implemented("cache stats", "P07")


@cache_app.command(name="verify")
def cache_verify() -> None:
    """Verify cache integrity against the recorded key hashes."""
    _not_implemented("cache verify", "P07")


@cache_app.command(name="prune")
def cache_prune() -> None:
    """Remove cache entries no longer referenced by any retained run."""
    _not_implemented("cache prune", "P07")


@cache_app.command(name="migrate")
def cache_migrate() -> None:
    """Migrate the cache to the current key-construction version."""
    _not_implemented("cache migrate", "P07")


@cache_app.command(name="export")
def cache_export() -> None:
    """Export the cache entries referenced by a run."""
    _not_implemented("cache export", "P07")


@baseline_app.command(name="update")
def baseline_update() -> None:
    """Refresh the committed CI regression baselines for a scenario directory."""
    _not_implemented("baseline update", "P17")


def main() -> None:
    """Run the AgentDX command-line interface.

    Guarantees: this is the only console entry point; it delegates argument
    parsing to Typer and never catches an exception it cannot classify.
    """
    app()
