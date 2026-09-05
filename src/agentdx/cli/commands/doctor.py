"""`agentdx doctor` (PRD §37.1, Design Constraint 4) — "makes the first five minutes survivable".

Every check here reads a fact this module did not invent: the Python version pin and the
`langgraph` pin both come from `pyproject.toml` (parsed, not duplicated as a literal —
AGENTS.md §4's "no magic numbers"), the `PYTHONHASHSEED=0` requirement is AGENTS.md §4.1's own
project-wide rule, and the store's migration state is `store.migrations.current_version`/
`latest_version` — the same functions `Store.open` itself consults. `doctor` composes checks;
it does not define new pass/fail thresholds of its own.

**Coverage gap, stated plainly (OP-2 first-pass finding #4 against `cli/`, `op2-audit-p17.md`
— not silently carried as an implicit gap the way it was before this note was added).** PRD
§37 names roughly nine checks; six are implemented here (`python-version`, `langgraph-version`,
`hash-seed`, `cache-db`, `store-migration`, `port`). **Not implemented**: data-dir writability,
SQLite WAL-mode support, DuckDB availability, cache *integrity* (`cache-db` only checks the
file exists, not that it opens cleanly), last-run determinism-quality/instrumentation-gap
reporting, and — the one PRD calls out twice, once by name in the checklist and once again as
its own Design Constraint 4 requirement — scanning for an API key present in `agentdx.toml` or
a committed file. `Check` also has no severity tier, so `doctor()`'s exit code can only ever be
`OK` or `USAGE_ERROR` (2) — PRD's own three-tier contract ("0 all pass · 1 warnings ·
2 failures") is collapsed to two; exit 1 is unreachable from this command today. Unlike this
codebase's other unbuilt CLI surfaces (`replay`, `export`, `cache *`, etc., all of which
self-declare via `_not_implemented(...)`), `doctor` had no equivalent disclosure until this
note — the six implemented checks are real and correct as far as they go, this is a genuine
coverage gap against the PRD's fuller list, not a defect in what exists.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer

from agentdx.cli._config import GlobalOptions, resolve_config
from agentdx.cli._exitcodes import OK, USAGE_ERROR
from agentdx.cli._output import Output
from agentdx.cli._target import find_repo_root
from agentdx.store.migrations import MigrationError, current_version, latest_version

__all__ = ["doctor"]


@dataclass(frozen=True, slots=True)
class Check:
    """One diagnostic's outcome — PRD §22.7's shape, applied to `doctor` as well as `--ci`."""

    name: str
    ok: bool
    detail: str
    fix: str | None = None


def _parse_version_pin(raw: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Parse a `pyproject.toml` `">=X.Y,<A.B"` pin into its two bounds. `None` if unparseable."""
    parts = [p.strip() for p in raw.split(",")]
    lower: tuple[int, int] | None = None
    upper: tuple[int, int] | None = None
    for part in parts:
        if part.startswith(">="):
            nums = part.removeprefix(">=").split(".")
            lower = (int(nums[0]), int(nums[1]))
        elif part.startswith("<"):
            nums = part.removeprefix("<").split(".")
            upper = (int(nums[0]), int(nums[1]))
    if lower is None or upper is None:
        return None
    return lower, upper


def _read_pins() -> dict[str, str]:
    """Read the `requires-python` and `langgraph` version pins from `pyproject.toml`.

    A plain-text scan, not a TOML parse — `doctor` must work even in an environment missing
    the project's own TOML dependency, and this reads exactly two well-known, single-line
    fields rather than the whole document.
    """
    root = find_repo_root()
    pins: dict[str, str] = {}
    if root is None:
        return pins
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return pins
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("requires-python"):
            pins["python"] = stripped.split("=", 1)[1].strip().strip('"')
        elif stripped.startswith('"langgraph'):
            inner = stripped.strip(",").strip('"')
            pins["langgraph"] = inner.removeprefix("langgraph")
    return pins


def _check_python(pins: dict[str, str]) -> Check:
    current = (sys.version_info.major, sys.version_info.minor)
    pin = pins.get("python")
    bounds = _parse_version_pin(pin) if pin is not None else None
    if bounds is None:
        return Check("python-version", True, f"running {sys.version.split()[0]} (no pin found)")
    lower, upper = bounds
    if lower <= current < upper:
        return Check("python-version", True, f"{sys.version.split()[0]} satisfies {pin!r}")
    return Check(
        "python-version",
        False,
        f"running {sys.version.split()[0]}, project pins {pin!r}",
        fix="install a Python in the pinned range and recreate the virtualenv (`uv sync`)",
    )


def _check_langgraph(pins: dict[str, str]) -> Check:
    try:
        installed = pkg_version("langgraph")
    except PackageNotFoundError:
        return Check(
            "langgraph-version",
            False,
            "langgraph is not installed",
            fix="run `uv sync` to install the pinned dependency set",
        )
    pin = pins.get("langgraph")
    bounds = _parse_version_pin(pin) if pin is not None else None
    if bounds is None:
        return Check("langgraph-version", True, f"installed {installed} (no pin found)")
    lower, upper = bounds
    nums = installed.split(".")
    current = (int(nums[0]), int(nums[1]))
    if lower <= current < upper:
        return Check("langgraph-version", True, f"{installed} satisfies {pin!r}")
    return Check(
        "langgraph-version",
        False,
        f"installed {installed}, project pins langgraph{pin!r}",
        fix="run `uv sync` to reconcile the installed version with pyproject.toml",
    )


def _check_hash_seed() -> Check:
    value = os.environ.get("PYTHONHASHSEED")
    if value == "0":
        return Check("hash-seed", True, "PYTHONHASHSEED=0")
    return Check(
        "hash-seed",
        False,
        f"PYTHONHASHSEED={value!r}, not '0' (AGENTS.md §4.1, invariant I1)",
        fix="export PYTHONHASHSEED=0 before running agentdx (set-hash-seed re-execs itself; "
        "this check only reports, it does not re-exec)",
    )


def _check_cache(data_dir: Path, cache_filename: str) -> Check:
    cache_path = data_dir.expanduser() / cache_filename
    if cache_path.is_file():
        return Check("cache-db", True, f"{cache_path} exists")
    return Check(
        "cache-db",
        True,
        f"{cache_path} does not exist yet — created on first `agentdx run` "
        "(not a failure; replay-mode runs against an empty cache will report E-CACHE-001)",
    )


def _check_store_migration(data_dir: Path) -> Check:
    store_path = data_dir.expanduser() / "agentdx.db"
    if not store_path.is_file():
        return Check("store-migration", True, f"{store_path} does not exist yet (fresh install)")
    try:
        conn = sqlite3.connect(str(store_path))
        try:
            current = current_version(conn)
        finally:
            conn.close()
    except (MigrationError, sqlite3.Error) as exc:
        return Check(
            "store-migration",
            False,
            f"{store_path}: {exc}",
            fix="back up the file, then run `agentdx cache migrate` "
            "or restore from a known-good backup",
        )
    latest = latest_version()
    if current == latest:
        return Check("store-migration", True, f"{store_path} at schema v{current} (latest)")
    if current < latest:
        return Check(
            "store-migration",
            False,
            f"{store_path} at schema v{current}, latest known is v{latest}",
            fix="run `agentdx cache migrate` to apply pending migrations",
        )
    return Check(
        "store-migration",
        False,
        f"{store_path} at schema v{current}, this build only knows v{latest} — newer than "
        "this build understands",
        fix="upgrade agentdx to a build that knows this schema version",
    )


def _check_port(host: str, port: int) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        result = sock.connect_ex((host, port))
    finally:
        sock.close()
    if result != 0:
        return Check("port", True, f"{host}:{port} is free")
    return Check(
        "port",
        False,
        f"{host}:{port} is already in use — `agentdx ui` would fail to bind",
        fix=f"stop whatever is listening on {port}, or pass `agentdx ui --port <other>`",
    )


def run_checks(*, config_options: GlobalOptions) -> tuple[Check, ...]:
    """Run every diagnostic and return the results, in a stable, documented order."""
    config = resolve_config(config_options)
    pins = _read_pins()
    return (
        _check_python(pins),
        _check_langgraph(pins),
        _check_hash_seed(),
        _check_cache(config.store.data_dir, config.cache.db_filename),
        _check_store_migration(config.store.data_dir),
        _check_port("127.0.0.1", config.api.port),
    )


def doctor(
    ctx: typer.Context,
    json_checks: Annotated[
        bool, typer.Option("--json", help="Emit checks as JSON (also the global --json flag).")
    ] = False,
) -> None:
    """Check the environment for anything that would silently break determinism."""
    options: GlobalOptions = ctx.obj["options"]
    out: Output = ctx.obj["output"]
    checks = run_checks(config_options=options)
    failed = [c for c in checks if not c.ok]

    if out.json_mode or json_checks:
        out.emit_json(
            {
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail, "fix": c.fix} for c in checks
                ],
                "ok": not failed,
            }
        )
    else:
        for check in checks:
            mark = out.style("✓", color="green") if check.ok else out.style("✗", color="red")
            out.line(f"{mark} {check.name}: {check.detail}")
            if not check.ok and check.fix:
                out.line(out.dim(f"    fix: {check.fix}"))

    raise typer.Exit(code=OK if not failed else USAGE_ERROR)
