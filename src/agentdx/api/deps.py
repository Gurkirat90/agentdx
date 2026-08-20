"""Server-wide state and the seams `runtime` would otherwise leak through (I3-adjacent).

`api/` may import `store` and `analysis` but never `runtime` (CONTEXT.md §4, `.importlinter`
`api-never-imports-runtime`) — the server launches a run as a subprocess and tails the event
table (PRD §24.2). This module is where that boundary is drawn concretely: `RunLauncher` and
`FaultController` are `Protocol`s declared here, exactly the shape `analysis.baseline.
BaselineExecutor` uses to let `analysis/` avoid importing `runtime/` while still being able to
*run* something — injection is what avoids the import, never a license to make it.

Both protocols default to `None` on `ApiState`. No prompt has built a concrete
implementation yet: `sdk.generic.RunHost` — "the half of `agentdx.run` that P06 owns" — does
not exist in this codebase (CONTEXT.md §7's P06 audit row; the gap was found, escalated, and
left for an owner decision that has not been made). `POST /api/runs` and `POST
/api/runs/{id}/faults` are implemented for real against these protocols and refuse cleanly
(`RunLaunchUnavailableError`/`503`, an unauthorized-shaped `403`) when no launcher/controller
is configured, rather than pretending to start or arm something they cannot execute — the
same "complete, schema-correct, tested decision engine with no live production call site"
shape `runtime/faults/transport.py`/`dependency.py` already carry (CONTEXT.md §5 row 9).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from fastapi import Request, WebSocket

from agentdx.config import AgentDXConfig, StoreConfig
from agentdx.store.sqlite import Store


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """What a `RunLauncher` hands back once a subprocess has actually started.

    `graph_hash` travels with `run_id`, not separately: PRD §6.1 defines `run_id` itself as
    `"r_" + 5 hex chars of a content hash` over `(seed, scenario_hash, graph_hash)`
    (`runtime.scheduler.make_run_id`), so whatever assigned `run_id` also necessarily
    resolved the graph to get there — the API asking for the hash again after the fact would
    just be asking the launcher to repeat work it already did.
    """

    run_id: str
    graph_hash: str


class RunLauncher(Protocol):
    """Starts a run as a subprocess (PRD §24.2). Declared here, implemented outside `api/`.

    `launch` returns the assigned identity, it is not given one: resolving *a user's
    instrumented graph* (as opposed to a fixture) is exactly the "capture" half of the
    system (PRD §6, `sdk`/`runtime`'s job), not something `api/` can do itself without
    importing `runtime`/`sdk` (`.importlinter` forbids both). This is I3's "explicit
    run-launcher service" concretely: the API hands over inputs and gets an already-assigned
    identity back, the same shape PRD §26.1's own `{scenario_id, mode, seed} -> run_id`
    signature describes for the endpoint as a whole.

    Guarantees a concrete implementation must provide: `launch` returns once the subprocess
    has been *started* and its identity is durably known (PRD §26.1: `POST /api/runs`
    returns in <200 ms; execution proceeds in the subprocess) — it does not wait for the run
    to finish, and any failure to even start the subprocess is raised synchronously so the
    route can report it rather than returning a run id for a run that will never write an
    event.
    """

    def launch(self, *, scenario_id: str, mode: str, seed: int, explore: bool) -> LaunchResult:
        """Start executing a new run out-of-process; return its assigned identity.

        Must not block until the run completes.
        """
        ...


@dataclass(frozen=True, slots=True)
class FaultArmResult:
    """What a `FaultController` hands back once a fault has actually been armed.

    `fault_id` is assigned by the controller, not the API: PRD §6.1 gives `Fault` its own
    identity (`fault_id`), realised as a `fault_injected` event the running subprocess
    writes — the API never writes events itself (`store/`'s append path is the writer's,
    per PRD §24.2), so it cannot mint an id for a record it will not create.
    """

    fault_id: str
    armed_at_virtual_ts: int


class FaultController(Protocol):
    """Arms a fault against a *live* run (PRD §26.1 `POST /api/runs/{id}/faults`).

    Declared here for the same reason as `RunLauncher`: arming a fault means signalling a
    running subprocess, which is a `runtime`-shaped operation the API is not permitted to
    perform by importing `runtime` directly (`.importlinter`).
    """

    def arm(
        self,
        *,
        run_id: str,
        fault_type: str,
        target: str,
        params: dict[str, object],
        immediate: bool,
    ) -> FaultArmResult:
        """Arm the fault; return its assigned `fault_id` and armed-at virtual timestamp."""
        ...


@dataclass(slots=True)
class ConcurrencyGuard:
    """In-process tracking of runs this server has launched, for the PRD §26.1 concurrency gate.

    Guarantees: counts only runs *this process* launched via `RunLauncher` — a second API
    process sharing the same store file (there should only ever be one, but nothing enforces
    that) is not this guard's concern, and the store's own `status='running'` count (read
    fresh on every `POST /api/runs`) is the actual, durable source of truth this guard is
    checked against, so a restarted server does not silently reset the effective limit.
    """

    _launched: set[str] = field(default_factory=set)

    def note_launched(self, run_id: str) -> None:
        """Record that this process launched `run_id`."""
        self._launched.add(run_id)

    def note_finished(self, run_id: str) -> None:
        """Forget a run this process launched, once it is no longer running."""
        self._launched.discard(run_id)


@dataclass(slots=True)
class WsConnectionGuard:
    """In-process per-run WebSocket connection counter, for PRD §26.2's "limit 8 per run".

    In-process is sufficient (unlike `ConcurrencyGuard`, which cross-checks the store): a
    WebSocket connection is a live TCP/ASGI object that only ever exists inside the process
    holding it, so there is no second, durable source of truth to reconcile against.
    """

    _counts: dict[str, int] = field(default_factory=dict)

    def try_connect(self, run_id: str, limit: int) -> bool:
        """Record a new connection for `run_id` and return True, unless `limit` is reached."""
        current = self._counts.get(run_id, 0)
        if current >= limit:
            return False
        self._counts[run_id] = current + 1
        return True

    def disconnect(self, run_id: str) -> None:
        """Release one connection slot for `run_id`."""
        current = self._counts.get(run_id, 0)
        if current <= 1:
            self._counts.pop(run_id, None)
        else:
            self._counts[run_id] = current - 1


@dataclass(slots=True)
class ApiState:
    """Everything a request needs beyond the store: config, and the two injectable seams.

    One instance lives on `app.state.agentdx` for the life of the process (PRD §24.2's "one
    process, one store file"). Immutable except for `concurrency`/`ws_connections`, whose
    whole job is to change.
    """

    store_path: Path
    config: AgentDXConfig
    run_launcher: RunLauncher | None = None
    fault_controller: FaultController | None = None
    concurrency: ConcurrencyGuard = field(default_factory=ConcurrencyGuard)
    ws_connections: WsConnectionGuard = field(default_factory=WsConnectionGuard)

    def open_store(self) -> Store:
        """Open a fresh, short-lived `Store` handle onto this server's data file.

        Guarantees: a new `sqlite3.Connection` per call, never shared across requests —
        `Store` documents itself as not thread-safe, and FastAPI may serve two requests
        concurrently on different threads (sync route handlers run in a threadpool). WAL mode
        (PRD §27.3) is exactly what makes many short-lived reader connections onto one file
        both correct and cheap; this mirrors PRD §24.2's own "two processes share the file"
        design one level down, inside a single process.
        """
        return Store.open(self.store_path, config=self.config.store)


def default_store_path(config: StoreConfig) -> Path:
    """Return the one data file this server's `Store` lives in (PRD §27.2: one file, many runs).

    Not a PRD-specified path — no prompt has built the CLI command that would normally decide
    it (`agentdx ui`, P17-adjacent). `<data_dir>/agentdx.db` is the same `data_dir` every
    other section of `agentdx.toml` already resolves through `AgentDXConfig`, so a user who
    has set `AGENTDX_STORE_DATA_DIR` gets a server pointed at the same place their `run`/
    `cache` commands would use, once those exist.
    """
    return config.data_dir.expanduser() / "agentdx.db"


def get_state(request: Request) -> ApiState:
    """FastAPI dependency: return this process's `ApiState` from `app.state` (HTTP routes)."""
    state: ApiState = request.app.state.agentdx
    return state


def get_state_ws(websocket: WebSocket) -> ApiState:
    """FastAPI dependency: return this process's `ApiState` from `app.state` (WS routes).

    Not `get_state` with a wider parameter type: a `Request`-typed dependency parameter is
    never satisfied on a WebSocket connection (confirmed directly — FastAPI's dependency
    solver raises `missing 1 required positional argument` when a websocket route depends on
    a callable typed `Request`, since the two are built from different parts of the ASGI
    scope), so a WS-side sibling naming `WebSocket` is the actual fix, not a formality.
    """
    state: ApiState = websocket.app.state.agentdx
    return state


def get_store(request: Request) -> Iterator[Store]:
    """FastAPI dependency: yield a fresh `Store`, closed when the request finishes."""
    state = get_state(request)
    store = state.open_store()
    try:
        yield store
    finally:
        store.close()


async def get_store_async(websocket: WebSocket) -> AsyncIterator[Store]:
    """Async FastAPI dependency: yield a fresh `Store` for a WebSocket connection's lifetime.

    `Store.open`/`close` are synchronous (plain `sqlite3`); this wrapper exists only so
    `ws.py`'s `async def` handler can depend on a `Store` the same way HTTP routes depend on
    `get_store`, without mixing a sync generator dependency into async code.
    """
    state = get_state_ws(websocket)
    store = state.open_store()
    try:
        yield store
    finally:
        store.close()


async def get_store_async_http(request: Request) -> AsyncIterator[Store]:
    """Async FastAPI dependency: yield a fresh `Store` for an `async def` HTTP route.

    Not `get_store` reused as-is: FastAPI resolves a *sync* generator dependency (`get_store`)
    in its threadpool, but runs an `async def` route body directly on the event loop's own
    thread — an `async def` route depending on `get_store` therefore opens its `sqlite3.
    Connection` on one thread and uses it on another, which `sqlite3` rejects outright
    (confirmed the hard way: `routes/runs.py::import_run`, the one genuinely `async def` HTTP
    route in this build because `UploadFile.read()` is itself async, raised
    `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that
    same thread` under `TestClient` before this dependency existed). Declaring this dependency
    `async def` keeps its whole body — including the synchronous `sqlite3.connect()` call
    `state.open_store()` makes — on the event loop thread the route itself runs on, the same
    fix `get_store_async` already applies for `ws.py`, just for `Request` instead of
    `WebSocket` (the two are not interchangeable dependency types — see `get_state_ws`'s own
    docstring for the confirmed reason).
    """
    state = get_state(request)
    store = state.open_store()
    try:
        yield store
    finally:
        store.close()
