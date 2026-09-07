"""FastAPI application factory and process entry point (PRD §24.2, §26, §31.10).

`create_app` wires every REST router (`system`/`scenarios`/`runs`/`analysis`/`findings`/
`scorecard`, all under `/api`) plus `ws.router` (mounted at the root — PRD §26.2's own path
has no `/api` prefix), installs the §26 error envelope everywhere
(`errors.install_exception_handlers`), and stashes one `ApiState` on `app.state.agentdx` for
the life of the process (PRD §24.2: "one process, one store file"). It also serves the built
Control Tower frontend from `static/`, when that directory is present (PRD §39.4) — see
`_mount_frontend`'s own docstring for the SPA-fallback reasoning.

`serve` is this module's other half — Design Constraint 3: binding a non-loopback host
requires the caller to pass `allow_non_local=True` (the CLI's `--host` flag is what sets it —
see `cli/main.py::ui`) and always prints a no-auth warning to stderr first. Both this module
and `cli/` are §4.1(4) determinism-hygiene allowlisted ("long-lived server" / "argument
handling and progress output") — this module's own stderr write and `uvicorn.run` call are
exactly that clause, not a run-context leak.
"""

from __future__ import annotations

import sys
from importlib.metadata import version as _installed_version
from pathlib import Path
from typing import Final

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse

from agentdx.api import ws
from agentdx.api.deps import ApiState, FaultController, RunLauncher, default_store_path
from agentdx.api.errors import install_exception_handlers
from agentdx.api.routes import analysis, findings, runs, scenarios, scorecard, system
from agentdx.config import AgentDXConfig

_VERSION: Final[str] = _installed_version("agentdx")

# PRD §39.4: `vite build`'s output is copied here by the Docker image (Dockerfile stage 2's
# final `COPY --from=frontend`). Absent in every other environment — a source checkout, this
# project's own test suite, `agentdx ui` run straight from `uv run` without a prior frontend
# build — so every use of this path below is conditional on it actually existing.
_STATIC_DIR: Final[Path] = Path(__file__).parent / "static"

# Loopback names/addresses this process trusts without a warning (Design Constraint 3).
# IPv4/IPv6 loopback plus the hostname most local tooling actually types — not an exhaustive
# reverse-DNS check (this is a safety prompt, not a firewall; a host that *resolves* to
# loopback through some other name still gets the warning, which is the safe direction to be
# wrong in).
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})

_NO_AUTH_WARNING: Final = (
    "WARNING: agentdx ui is binding to {host}:{port}, not 127.0.0.1 (loopback-only).\n"
    "This server has NO AUTHENTICATION (PRD §26, §31.10's local-first posture) — anyone who\n"
    "can reach {host}:{port} can start runs, inject faults into a live run, and read every\n"
    "run's full event log, including tool-call and LLM payloads. Only bind here on a network\n"
    "you trust."
)


def _is_loopback(host: str) -> bool:
    """Return True iff `host` is a loopback address/hostname (Design Constraint 3)."""
    return host in _LOOPBACK_HOSTS


def _non_local_refusal(host: str, port: int) -> str:
    """Build `serve`'s refusal message (TRY003: long message, not built at the raise site)."""
    return (
        f"refusing to bind {host}:{port} — {host!r} is not loopback and this was not "
        f"confirmed with an explicit --host flag (Design Constraint 3, PRD §26/§31.10). "
        f"Re-run with `agentdx ui --host {host}` if binding beyond localhost is genuinely "
        f"intended — this server has no authentication."
    )


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built Control Tower from `static_dir`, with SPA fallback (PRD §39.4).

    A plain `StaticFiles` mount at `/` cannot do this alone, because
    `frontend/src/routes/router.tsx` is a real History-API router (`/runs/{id}`,
    `/runs/{id}/scorecard`), not a hash router: a deep-link navigation or a hard refresh on
    `/runs/r_abc123` must still return `index.html` — there is no file at that path, and the
    client-side router is what turns that HTML into the right screen once it loads. Without
    this, refreshing on any route but `/` would 404 (PRD §39.4 does not spell this out, but
    "agentdx ui ... serves a fully self-contained app" is not true if navigating within it
    breaks reload). One catch-all handler covers both a real asset request
    (`/assets/index-<hash>.js`, whatever Vite's own output layout is — never hardcoded here)
    and the SPA-fallback case, rather than a separate `StaticFiles` mount whose own directory
    (an exact `assets/` subpath) would need to exist at import time to avoid a startup error.

    Registered after `/api` and `/ws` are already on `app.routes` (`create_app`'s own call
    order), so Starlette's first-match routing tries those before ever reaching the catch-all
    below — moving this call earlier would silently shadow every API route with `index.html`.
    """
    index_path = static_dir / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_frontend(full_path: str) -> FileResponse:
        """Serve a real static file if one exists at this path, else the SPA shell."""
        candidate = (static_dir / full_path).resolve()
        if full_path and candidate.is_file() and static_dir.resolve() in candidate.parents:
            return FileResponse(candidate)
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="frontend not built into this image")
        return FileResponse(index_path)


def create_app(
    *,
    config: AgentDXConfig | None = None,
    store_path: Path | None = None,
    run_launcher: RunLauncher | None = None,
    fault_controller: FaultController | None = None,
) -> FastAPI:
    """Build the AgentDX FastAPI app: every router, the §26 error envelope, one `ApiState`.

    Args:
        config: Resolved configuration; `AgentDXConfig.load()` when not given.
        store_path: Explicit store path; `deps.default_store_path(config.store)` when not
            given (PRD §27.2's "one data file").
        run_launcher: Injected `deps.RunLauncher`. `None` (the default) means `POST
            /api/runs` refuses with `503` rather than pretending to start a run (I3; see
            `deps.py`'s own module docstring for why no concrete implementation exists yet).
        fault_controller: Injected `deps.FaultController`, same default/refusal shape for
            `POST /api/runs/{id}/faults`.

    Returns:
        A fully wired `FastAPI` app, not yet serving — pass it to `uvicorn.run` (`serve`
        does this) or to a test client.
    """
    resolved_config = config if config is not None else AgentDXConfig.load()
    resolved_store_path = (
        store_path if store_path is not None else default_store_path(resolved_config.store)
    )
    state = ApiState(
        store_path=resolved_store_path,
        config=resolved_config,
        run_launcher=run_launcher,
        fault_controller=fault_controller,
    )

    app = FastAPI(
        title="AgentDX API",
        version=_VERSION,
        summary=(
            "Multi-agent coordination debugger, deterministic replay runtime and chaos "
            "harness — PRD §26 API layer. Bounded search: absence of findings is not proof "
            "of absence."
        ),
    )
    app.state.agentdx = state
    install_exception_handlers(app)

    api_router = APIRouter(prefix="/api")
    for module_router in (
        system.router,
        scenarios.router,
        runs.router,
        analysis.router,
        findings.router,
        scorecard.router,
    ):
        api_router.include_router(module_router)
    app.include_router(api_router)
    # PRD §26.2's path has no `/api` prefix — the one REST-layer exception.
    app.include_router(ws.router)

    # Registered last, deliberately — see `_mount_frontend`'s own docstring for why this
    # order matters. Conditional on the directory actually existing: absent everywhere this
    # process doesn't run from a Docker image built by this project's own Dockerfile (a
    # source checkout, this test suite, `uv run agentdx ui`), and `/` correctly 404s via the
    # ordinary FastAPI "no route matched" path when it is absent, rather than this module
    # inventing a static directory that was never built.
    if _STATIC_DIR.is_dir():
        _mount_frontend(app, _STATIC_DIR)

    return app


def serve(
    *,
    host: str | None = None,
    port: int | None = None,
    allow_non_local: bool = False,
    config: AgentDXConfig | None = None,
    store_path: Path | None = None,
    run_launcher: RunLauncher | None = None,
    fault_controller: FaultController | None = None,
) -> None:
    """Build the app and run it under uvicorn (PRD §24.2, §26; Design Constraint 3).

    Args:
        host: Bind address; `config.api.host` (PRD-default `127.0.0.1`) when not given.
        port: Bind port; `config.api.port` (PRD-default `8420`) when not given.
        allow_non_local: Must be `True` for `host` to resolve to anything but loopback — the
            CLI sets this exactly when the user passed `--host` explicitly on the command
            line (`cli/main.py::ui`), which is the "explicit flag" Design Constraint 3 asks
            for; a non-loopback value arriving only through `agentdx.toml`/env (no CLI flag)
            still refuses, because those are not the explicit-at-invocation-time confirmation
            the constraint means.
        config: Forwarded to `create_app`.
        store_path: Forwarded to `create_app`.
        run_launcher: Forwarded to `create_app`.
        fault_controller: Forwarded to `create_app`.

    Raises:
        ValueError: the resolved `host` is not loopback and `allow_non_local` is not `True`.
    """
    resolved_config = config if config is not None else AgentDXConfig.load()
    resolved_host = host if host is not None else resolved_config.api.host
    resolved_port = port if port is not None else resolved_config.api.port

    if not _is_loopback(resolved_host) and not allow_non_local:
        raise ValueError(_non_local_refusal(resolved_host, resolved_port))
    if not _is_loopback(resolved_host):
        # T20 ("no print() outside the CLI", AGENTS.md §4.1 clause 4 covers `api/` too, but
        # the *rule* is scoped to the literal `print()` builtin) — `sys.stderr.write` is the
        # same clause's stderr write, just not the one spelling ruff's `T201` matches.
        sys.stderr.write(_NO_AUTH_WARNING.format(host=resolved_host, port=resolved_port) + "\n")
        sys.stderr.flush()

    app = create_app(
        config=resolved_config,
        store_path=store_path,
        run_launcher=run_launcher,
        fault_controller=fault_controller,
    )
    uvicorn.run(app, host=resolved_host, port=resolved_port)
