"""The PRD §26 error envelope, everywhere — the E-XXX-NNN codes of PRD §36 on the wire.

Design constraint 2 (the P14 prompt): "The error envelope from §26 is used everywhere, with
the §36 code and a docs link. No bare 500s, no stack traces to the client." This module is
where that is mechanised: `ApiError` and its subclasses carry a stable code and an HTTP
status; `install_exception_handlers` registers FastAPI handlers that turn *every* exception —
one of ours, a `store`/`analysis`/`scenario` module's own coded error, or a genuine internal
defect — into `{"error": {...}}`, never a framework-default traceback page.

Codes reuse the subsystem's own, where one already exists (`StoreError.code`,
`BundleError.code`, `ScenarioLoadError.code`, one of `ScenarioValidationError.errors[0].code`,
`ConfigError`'s embedded `E-CONFIG-001`) — this module does not mint a second opinion of what
a `store/` or `scenario/` failure is called. It mints new codes only for conditions PRD §36
names without an existing implementation to borrow one from (`E-RUN-404`, `E-RUN-409`,
`E-CMP-001`, `E-FAULT-001`), plus the generic `E-API-<status>` fallback PRD §36's own table
row literally spells as `E-API-4xx/5xx` for anything else.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agentdx.analysis.overhead import OverheadAnalysisError
from agentdx.analysis.timing import TimingAnalysisError
from agentdx.config import ConfigError
from agentdx.events.canonical import (
    FloatNotPermittedError,
    MalformedEventLineError,
    UnsupportedValueError,
)
from agentdx.scenario.loader import ScenarioLoadError
from agentdx.scenario.validate import ScenarioValidationError
from agentdx.store import StoreError
from agentdx.store.bundle import BundleError

_DOCS: Final = "docs/api.md"
_log = logging.getLogger("agentdx.api")


def docs_url(code: str) -> str:
    """Return the `docs/api.md` anchor for an `E-XXX-NNN` code, lower-cased (PRD §26)."""
    return f"{_DOCS}#{code.lower()}"


class ApiError(Exception):
    """Base of every error this module raises directly, carrying its own HTTP status.

    Guarantees: `code` is always `E-XXX-NNN`-shaped (not enforced mechanically here — every
    subclass below is a fixed, reviewed literal, the same trust level `StoreError`/
    `BundleError` place in their own call sites); `detail` is JSON-serialisable.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(self, code: str, message: str, *, detail: dict[str, object] | None = None) -> None:
        """Build the error from its stable code, a user-facing message, and structured detail."""
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(f"[{code}] {message}")


class RunNotFoundError(ApiError):
    """`E-RUN-404` — PRD §26.1 `GET /api/runs/{id}` and every sibling `{id}` route."""

    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, run_id: str) -> None:
        """Build the error naming the missing `run_id`."""
        super().__init__("E-RUN-404", f"Run {run_id} not found", detail={"run_id": run_id})


class TooManyConcurrentRunsError(ApiError):
    """`E-RUN-409` — PRD §26.1 `POST /api/runs`: the concurrent-run limit was exceeded."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, limit: int, running: int) -> None:
        """Build the error naming the configured limit and the current running count."""
        super().__init__(
            "E-RUN-409",
            f"{running} runs are already in progress (limit {limit})",
            detail={"limit": limit, "running": running},
        )


class RunNotRunningError(ApiError):
    """`409` — PRD §26.1 `POST /api/runs/{id}/faults`: "Run is `running`"."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, run_id: str, actual_status: str) -> None:
        """Build the error naming the run and the status a fault cannot be injected into."""
        super().__init__(
            "E-RUN-409",
            f"Run {run_id} is {actual_status}, not running — a fault can only be injected "
            f"into a run in progress",
            detail={"run_id": run_id, "status": actual_status},
        )


class ScenarioNotFoundError(ApiError):
    """`E-SCEN-404` — a scenario id names neither a persisted nor a discoverable scenario."""

    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, scenario_id: str) -> None:
        """Build the error naming the missing `scenario_id`."""
        super().__init__(
            "E-SCEN-404", f"Scenario {scenario_id} not found", detail={"scenario_id": scenario_id}
        )


class FaultTargetNotFoundError(ApiError):
    """`E-FAULT-001` — PRD §36: "Fault target not found"."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, target: str, run_id: str) -> None:
        """Build the error naming the missing fault target and the run it was aimed at."""
        super().__init__(
            "E-FAULT-001",
            f"Fault target {target!r} not found in run {run_id}",
            detail={"target": target, "run_id": run_id},
        )


class ChaosAuthorizationError(ApiError):
    """`E-CHAOS-001` — PRD §36 / §13.3: outside the declared blast radius, or no opt-in.

    Three named constructors, not a free-text `message` parameter (TRY003: a long message
    belongs to the exception class, not the raise site) — one per I12/§13.3 refusal a chaos
    fault against a user graph can hit: opt-in missing, blast radius declared but empty, or a
    target outside what was declared.
    """

    status_code = status.HTTP_403_FORBIDDEN

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        """Build the error from a message and detail; prefer the named constructors above."""
        super().__init__("E-CHAOS-001", message, detail=detail)

    @classmethod
    def opt_in_required(cls) -> ChaosAuthorizationError:
        """The scenario targets a user graph but has not set `chaos_opt_in: true`."""
        return cls(
            "this run's scenario targets a user graph and has not set `chaos_opt_in: true` "
            "— an interactive fault cannot bypass §13.3",
            detail={"target": "graph"},
        )

    @classmethod
    def blast_radius_empty(cls, blast_radius: object) -> ChaosAuthorizationError:
        """The scenario opted in but declared no blast radius at all."""
        return cls(
            "this scenario's blast_radius is empty — a user-graph fault must be explicitly "
            "declared in scope (§13.4)",
            detail={"blast_radius": blast_radius},
        )

    @classmethod
    def target_outside_blast_radius(
        cls, target: str, blast_radius: object
    ) -> ChaosAuthorizationError:
        """The requested target is not among the scenario's declared blast radius."""
        return cls(
            f"target {target!r} is outside the declared blast radius",
            detail={"target": target, "blast_radius": blast_radius},
        )


class ScenarioUnresolvableForChaosError(ApiError):
    """`409 E-CHAOS-004` — a run's scenario is set but cannot be resolved right now.

    I12 (CONTEXT.md §2) requires a user-graph fault to be explicitly authorized via
    `chaos_opt_in`/`blast_radius`; that check (`_check_chaos_authorization`) can only run
    once the run's scenario has been re-parsed into `resolved`. If the scenario row this run
    is pinned to (`RunRecord.scenario_id`) has gone missing from the store, or the stored
    text no longer parses, this build has no way to tell a fixture target from a user-graph
    one — and PRD §36 rule 1 ("never fail silently") plus I12 itself both require that
    ambiguity to refuse the fault, not silently skip authorization and arm it anyway. Raised
    only when `scenario_id` is set but unresolvable — a run with no `scenario_id` at all
    (never populated) is unaffected by this error; see this error's call site for why.
    """

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, run_id: str, scenario_id: str) -> None:
        """Build the error naming the run and the scenario id that could not be resolved."""
        super().__init__(
            "E-CHAOS-004",
            f"Run {run_id}'s scenario {scenario_id!r} could not be resolved — fault "
            f"injection is refused because chaos-safety authorization (I12) cannot be "
            f"verified without it",
            detail={"run_id": run_id, "scenario_id": scenario_id},
        )


class UnknownFaultTypeError(ApiError):
    """`400` — PRD §26.1 `POST /api/runs/{id}/faults`: "unknown fault type"."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, fault_type: str) -> None:
        """Build the error naming the unrecognised fault type."""
        super().__init__(
            "E-FAULT-002", f"Unknown fault type {fault_type!r}", detail={"type": fault_type}
        )


class CompareIncomparableError(ApiError):
    """`E-CMP-001` — PRD §26.1 `POST /api/runs/compare`: scenario hashes differ, no `force`."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, run_a: str, run_b: str) -> None:
        """Build the error naming the two runs whose scenario hashes disagree."""
        super().__init__(
            "E-CMP-001",
            f"{run_a} and {run_b} have different scenario hashes — pass force=true to "
            f"compare anyway",
            detail={"run_a": run_a, "run_b": run_b},
        )


class NotYetAvailableError(ApiError):
    """`202`-adjacent `409` — data this endpoint would serve has not been computed yet.

    Used where PRD §26.1 promises a shape (e.g. `GET /runs/{id}/exploration`) that only a
    not-yet-built orchestration prompt (P17) populates in this codebase today — see
    `docs/api.md` "What this build does not yet do". Distinct from `E-RUN-404` (the run
    itself does not exist) and from a silent empty/default response (PRD §36 rule 1: never
    fail silently) — the client is told plainly that the data does not exist yet, and why.
    """

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, code: str, message: str, *, detail: dict[str, object] | None = None) -> None:
        """Build the error from a caller-supplied code, message and detail."""
        super().__init__(code, message, detail=detail)


class RunLaunchUnavailableError(ApiError):
    """`503` — no `RunLauncher` is wired to this server process.

    PRD §24.2: the API launches runs as a subprocess. This build's `runtime`/`cli` layers
    have no `agentdx run` / `RunHost` yet to launch (CONTEXT.md §7: "the `RunHost` gap...
    left unresolved by owner instruction" — a P06/P17 gap, not P14's to guess at). A server
    configured with `run_launcher=None` (the default) refuses `POST /api/runs` with this
    error rather than pretending to have started a run it cannot execute.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        """Build the fixed, argument-free error — there is nothing request-specific to report."""
        super().__init__(
            "E-RUN-503",
            "No run launcher is configured on this server — launching a run requires "
            "`agentdx run`/`RunHost` (CONTEXT.md §7), not yet built in this codebase",
        )


class FaultControlUnavailableError(ApiError):
    """`503` — no `FaultController` is wired to this server process.

    Same shape as `RunLaunchUnavailableError`: arming a fault against a live run means
    signalling a running subprocess, which nothing in this codebase does yet (`runtime/
    faults/`'s `transport.py`/`dependency.py` are "complete, schema-correct, tested decision
    engines with no live production call site" — CONTEXT.md §5 row 9).
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        """Build the fixed, argument-free error — there is nothing request-specific to report."""
        super().__init__(
            "E-CHAOS-503",
            "No fault controller is configured on this server — arming a fault against a "
            "live run requires a runtime hook this codebase does not have yet",
        )


def _envelope(code: str, message: str, detail: dict[str, object]) -> dict[str, object]:
    return {"error": {"code": code, "message": message, "detail": detail, "docs": docs_url(code)}}


def install_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that make the §26 envelope the *only* error shape this app emits.

    Order matters only in that FastAPI dispatches by the most specific matching type; each
    handler below is registered for a disjoint exception family, so there is no shadowing.
    """

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(StoreError)
    async def _handle_store_error(_request: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for_store_code(exc.code),
            content=_envelope(exc.code, str(exc), {}),
        )

    @app.exception_handler(BundleError)
    async def _handle_bundle_error(_request: Request, exc: BundleError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope(exc.code, str(exc), {}),
        )

    @app.exception_handler(ScenarioLoadError)
    async def _handle_scenario_load_error(
        _request: Request, exc: ScenarioLoadError
    ) -> JSONResponse:
        detail: dict[str, object] = {} if exc.line is None else {"line": exc.line}
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope(exc.code, exc.message, detail),
        )

    @app.exception_handler(ScenarioValidationError)
    async def _handle_scenario_validation_error(
        _request: Request, exc: ScenarioValidationError
    ) -> JSONResponse:
        first = exc.errors[0]
        detail: dict[str, object] = {
            "errors": [
                {
                    "code": e.code,
                    "path": e.path,
                    "message": e.message,
                    "suggestion": e.suggestion,
                    "file": e.file,
                    "line": e.line,
                }
                for e in exc.errors
            ]
        }
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope(first.code, first.message, detail),
        )

    @app.exception_handler(TimingAnalysisError)
    @app.exception_handler(OverheadAnalysisError)
    async def _handle_timing_error(_request: Request, exc: Exception) -> JSONResponse:
        # Both carry `.code`/`E-ANLZ-0NN` — raised when a run's log is not yet in a shape
        # this analysis can run over (e.g. a still-running run has an unterminated span).
        # 409: the request is well-formed, the run's *current state* precludes it.
        code = getattr(exc, "code", "E-ANLZ-000")
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_envelope(code, str(exc), {}),
        )

    @app.exception_handler(ConfigError)
    async def _handle_config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope("E-CONFIG-001", str(exc), {}),
        )

    @app.exception_handler(FloatNotPermittedError)
    @app.exception_handler(MalformedEventLineError)
    @app.exception_handler(UnsupportedValueError)
    async def _handle_canonical_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(
                "E-API-500",
                "Internal error: a stored event could not be decoded. Please file a bug "
                "report with the run id.",
                {},
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope("E-API-422", "Request validation failed", {"errors": exc.errors()}),
        )

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
        code = f"E-API-{exc.status_code}"
        # `fastapi.exceptions.HTTPException.__init__` types its own `detail` parameter as
        # `Any` (any JSON value is accepted — dicts included, e.g. Starlette's own internal
        # exceptions), but that parameter flows into `starlette.exceptions.HTTPException.
        # __init__`, whose *own* local narrowing (`str | None` -> `str`) is what mypy infers
        # the `.detail` attribute's static type from — so it reports `str` and calls this
        # `isinstance` check redundant. Traced through both stubs: the check is real at
        # runtime, not merely mypy being cautious, so this is a deliberate, narrow suppression
        # of a stub inaccuracy, not an escape hatch on code this project wrote.
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"  # type: ignore[redundant-expr]
        return JSONResponse(status_code=exc.status_code, content=_envelope(code, message, {}))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # AGENTS.md §4: never a bare `except:`, never a swallowed exception — logged with
        # its real traceback server-side, but the client sees no stack trace (design
        # constraint 2), only "Internal error", reserved language per PRD §36 rule 4.
        _log.exception("unhandled exception serving %s", _request.url)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(
                "E-API-500",
                "Internal error. Please file a bug report.",
                {},
            ),
        )


def _status_for_store_code(code: str) -> int:
    """Map a `StoreError` code to an HTTP status, defaulting to `400`.

    `E-STORE-001`/`E-STORE-002` (not a store / schema too new) are treated as `404`-adjacent
    "the referenced data cannot be read", matching how those codes surface through a `run_id`
    lookup in practice; every other `StoreError` is a request the client made incorrectly
    (bad batch, sealed run, unknown status), `400`.
    """
    if code in ("E-STORE-001", "E-STORE-002", "E-STORE-008"):
        return status.HTTP_404_NOT_FOUND
    return status.HTTP_400_BAD_REQUEST
