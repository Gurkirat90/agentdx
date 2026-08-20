"""`WS /ws/runs/{id}` — the live event/finding/verdict stream (PRD §26.2).

Not mounted under `/api` (`app.py` mounts this router at the root): PRD §26.2 gives the path
as `/ws/runs/{id}`, not `/api/ws/runs/{id}` — every REST endpoint in this build lives under
`/api` (PRD §26.1's own table), the WebSocket path is the one exception the PRD itself draws.

Backlog and live tailing are one continuous cursor, not two code paths bolted together:
`_sender` reads `from_seq` forward through `store.read_events` in chunks until the store
genuinely has nothing more *right now*, sends what it found as `"events"` (batched, PRD
§26.2's "Sent in batches of 1000 before any live event"), then keeps re-issuing the same read
with an advancing `from_seq` — sent as `"event"`, one at a time — polling at
`config.ws_poll_interval_s` when nothing new has landed. The seq cursor is what makes "no
duplicates, no gaps" true both across the backlog/live boundary and across a reconnect (PRD
§26.2 "Reconnect"): a client that resumes with `from_seq = last_seen + 1` gets exactly the
read this function would have produced had the first connection never dropped, because
nothing about `from_seq`'s meaning depends on which "mode" produced it.

`finding`/`verdict`/`status` pushes poll the store the same way (`store.list_findings`/
`store.get_scorecard`/`store.get_run(...).status`) — there is no writer-to-API push channel
in this build (`store/` is a plain SQLite file, PRD §27.3's "one writer, many readers"), so
"pushed" here means "noticed on the next poll", the same honest constraint
`config.ApiConfig.ws_poll_interval_s` documents. `finding` pushes are real and can fire:
`store/bundle.py`'s `import_bundle` calls `store.upsert_finding` for real, so a WS client
watching a run that gets findings imported mid-connection sees them arrive. `verdict` is
protocol-complete but not expected to fire in this build: no orchestration prompt (P17) ever
calls `store.upsert_scorecard` (grepped — zero production call sites), the same declared gap
`RunDetail.verdict` (`routes/runs.py`) and `GET .../exploration` (`routes/analysis.py`)
already carry; this route still polls for one honestly rather than omitting the message type,
because the moment a scorecard row exists this code delivers it correctly (a "complete,
schema-correct, tested... no live production call site" shape — CONTEXT.md §5 row 9).

Backpressure sampling (PRD §26.2: "If the client cannot keep up... switches to sampled
mode") is a declared, undocumented-by-the-PRD heuristic — see `_maybe_update_sampling`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Annotated, Final

from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from agentdx.api.deps import ApiState, get_state_ws, get_store_async
from agentdx.api.routes.findings import _project as _project_finding
from agentdx.api.routes.runs import _project_event
from agentdx.config import ApiConfig
from agentdx.events.schema import SCHEMA_VERSION, Event
from agentdx.store.sqlite import Store

router = APIRouter()

_log = logging.getLogger("agentdx.api.ws")

# PRD §26.2's close-code table.
_CLOSE_NORMAL: Final = 1000
_CLOSE_RUN_NOT_FOUND: Final = 4004
_CLOSE_TOO_MANY_CONNECTIONS: Final = 4013

# Not a PRD §26.2 number (the PRD names the *behaviour*, not a detection threshold or an
# escalation curve) — a declared heuristic local to this module rather than a `config.py`
# field, unlike the `ws_*` settings `ApiConfig` documents as "every WebSocket protocol
# number" (those all come from the PRD's own table; these two do not). Three consecutive
# flow-control pauses while *live*-tailing (backlog sending already blocks on flow control by
# design — a pause there is not evidence of trouble) doubles the sample rate, starting at 2,
# capped at 64.
_SAMPLING_PAUSE_THRESHOLD: Final = 3
_SAMPLING_MAX_N: Final = 64


@dataclass(slots=True)
class _Session:
    """Mutable per-connection state the receiver and sender coroutines both read and write.

    One instance per WS connection. `subscribed` gates the sender until the client's first
    `subscribe` arrives (PRD §26.2 lists it as the first client message); `done` is how
    either side tells the other to stop, including waking the sender out of a flow-control
    pause or a poll wait promptly rather than on the next timeout; `ack_event` is how the
    sender wakes from a flow-control pause the instant an `ack` arrives rather than polling.
    """

    run_id: str
    subscribed: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    from_seq: int = 0
    types: frozenset[str] | None = None
    acked_through: int = -1
    ack_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_sent_seq: int = -1
    sampling_n: int | None = None
    consecutive_pauses: int = 0


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    """Send a PRD §26.2-shaped WS error frame.

    Not the REST §26 envelope (`errors.py`'s `{"error": {...}}`) — the PRD's own WS example
    spells a different, flatter shape: `{"type":"error","code":"...","message":"..."}`, with
    no `detail`/`docs` fields. This mirrors that shape exactly rather than reusing the REST
    one, because the two are documented as genuinely different wire formats.
    """
    await websocket.send_json({"type": "error", "code": code, "message": message})


async def _handle_subscribe(
    websocket: WebSocket, session: _Session, raw: dict[str, object]
) -> None:
    """Apply a client `subscribe` message to `session`; honoured once per connection.

    A `subscribe` after the first is a silent no-op — PRD §26.2's own example subscribes
    exactly once per connection and describes no semantics for changing `from_seq`/`filters`
    mid-stream; `_sender` already reads `session.from_seq`/`session.types` a single time,
    before its backlog loop starts, so a second `subscribe`'s values would never be observed
    even if stored.
    """
    if session.subscribed.is_set():
        return
    from_seq = raw.get("from_seq", 0)
    if not isinstance(from_seq, int) or from_seq < 0:
        await _send_error(websocket, "E-WS-003", "from_seq must be a non-negative integer")
        from_seq = 0
    types: frozenset[str] | None = None
    filters = raw.get("filters")
    if isinstance(filters, dict):
        raw_types = filters.get("types")
        if isinstance(raw_types, list) and all(isinstance(t, str) for t in raw_types):
            types = frozenset(raw_types)
        elif raw_types is not None:
            await _send_error(websocket, "E-WS-003", "filters.types must be a list of strings")
    session.from_seq = from_seq
    session.types = types
    session.subscribed.set()


async def _handle_ack(websocket: WebSocket, session: _Session, raw: dict[str, object]) -> None:
    """Apply a client `ack` message to `session` (PRD §26.2 flow control)."""
    through = raw.get("through_seq")
    if not isinstance(through, int):
        await _send_error(websocket, "E-WS-003", "through_seq must be an integer")
        return
    if through > session.acked_through:
        session.acked_through = through
        session.ack_event.set()


async def _receiver(websocket: WebSocket, session: _Session, config: ApiConfig) -> None:
    """Handle client -> server messages: `subscribe`/`ack`/`unsubscribe`/`ping` (PRD §26.2).

    Ends (returning, which the route handler reads as "time to close") on `unsubscribe`, on
    `config.ws_heartbeat_timeout_s` of silence (PRD §26.2: "server closes after 45s of
    silence" — any client message counts as activity, not only `ping`; the clause exists to
    detect a dead connection, and any traffic proves this one is alive), or when the client
    disconnects.
    """
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_json(), timeout=config.ws_heartbeat_timeout_s
                )
            except TimeoutError:
                _log.info(
                    "ws %s: closing after %.0fs of silence (PRD Sec26.2 heartbeat)",
                    session.run_id,
                    config.ws_heartbeat_timeout_s,
                )
                return
            if not isinstance(raw, dict):
                await _send_error(websocket, "E-WS-001", "message must be a JSON object")
                continue
            msg_type = raw.get("type")
            if msg_type == "subscribe":
                await _handle_subscribe(websocket, session, raw)
            elif msg_type == "ack":
                await _handle_ack(websocket, session, raw)
            elif msg_type == "unsubscribe":
                return
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await _send_error(websocket, "E-WS-002", f"unknown message type {msg_type!r}")
    except WebSocketDisconnect:
        return
    finally:
        session.done.set()


def _event_json(event: Event) -> dict[str, object]:
    """Project one event to its WS wire shape — the same `EventOut` REST already returns."""
    return _project_event(event).model_dump(mode="json")


def _read_batch(
    store: Store, run_id: str, from_seq: int, max_matches: int, types: frozenset[str] | None
) -> tuple[list[Event], int, bool]:
    """Read forward from `from_seq` until `max_matches` matches are found, or the log runs dry.

    Returns `(matched_events, next_from_seq, exhausted)`. `next_from_seq` is always the seq
    immediately after the last event *scanned* — matched or not (mirrors `routes.runs.
    get_events`'s own `next_from_seq` bookkeeping), so a filtered subscribe never re-scans
    events it has already looked at and rejected, on the next call. `exhausted` is True iff
    `store.read_events` ran dry before `max_matches` matches were found, meaning the store had
    nothing further *as of this read* — the caller's signal to stop batching (backlog) or to
    sleep (live).

    Streams one event at a time from `store.read_events` — never materialises more than
    `max_matches` events at once (Design Constraint 6), even when `types` is sparse enough
    that satisfying `max_matches` requires scanning far more rows than that.
    """
    matched: list[Event] = []
    next_from_seq = from_seq
    exhausted = True
    for event in store.read_events(run_id, from_seq=from_seq):
        next_from_seq = event.seq + 1
        if types is None or event.type.value in types:
            matched.append(event)
        if len(matched) >= max_matches:
            exhausted = False
            break
    return matched, next_from_seq, exhausted


async def _await_flow_control(session: _Session, max_unacked: int) -> bool:
    """Block while more than `max_unacked` sent events are unacked; return whether it had to.

    PRD §26.2 "Flow control" is measured in `seq` space (the same space `ack.through_seq`
    names), not delivered-message count — `session.last_sent_seq - session.acked_through` is
    exactly that gap, so a filtered subscribe delivering few messages per scanned seq range is
    never penalised for seq numbers it never actually sent.

    Relies on the route handler cancelling this coroutine's task (via the enclosing `_sender`
    task) once the receiver ends, rather than a timeout loop here — an `ack` that will never
    come (client gone) is indistinguishable from "still connected but slow" without that
    external signal, and the route handler already has it.
    """
    paused = False
    while session.last_sent_seq - session.acked_through > max_unacked:
        paused = True
        session.ack_event.clear()
        await session.ack_event.wait()
    return paused


async def _maybe_update_sampling(session: _Session, websocket: WebSocket) -> None:
    """Escalate to sampled mode after repeated flow-control pauses while live (PRD §26.2)."""
    if session.consecutive_pauses < _SAMPLING_PAUSE_THRESHOLD:
        return
    next_n = 2 if session.sampling_n is None else min(session.sampling_n * 2, _SAMPLING_MAX_N)
    if next_n == session.sampling_n:
        return
    session.sampling_n = next_n
    session.consecutive_pauses = 0
    await websocket.send_json({"type": "status", "sampling": next_n})


async def _send_backlog(
    websocket: WebSocket, session: _Session, store: Store, config: ApiConfig
) -> int:
    """Send the backlog in batches of `config.ws_backlog_batch_size`; return the resume `from_seq`.

    PRD §26.2: "Sent in batches of 1000 before any live event; ordering is seq-monotonic
    across the boundary" — this drains `store.read_events` from `session.from_seq` until it
    genuinely runs dry, applying flow control before every batch, then hands the resuming
    cursor to `_send_live` unchanged.
    """
    from_seq = session.from_seq
    while True:
        matched, next_from_seq, exhausted = _read_batch(
            store, session.run_id, from_seq, config.ws_backlog_batch_size, session.types
        )
        if matched:
            await _await_flow_control(session, config.ws_flow_control_max_unacked)
            if session.done.is_set():
                return from_seq
            await websocket.send_json(
                {
                    "type": "events",
                    "events": [_event_json(e) for e in matched],
                    "from_seq": matched[0].seq,
                    "to_seq": matched[-1].seq,
                }
            )
            session.last_sent_seq = matched[-1].seq
        from_seq = next_from_seq
        if exhausted or session.done.is_set():
            return from_seq


async def _send_live(
    websocket: WebSocket, session: _Session, store: Store, state: ApiState, from_seq: int
) -> None:
    """Tail new events one at a time, and poll for findings/verdict/status changes (PRD §26.2).

    Reuses `config.ws_backlog_batch_size` as the internal *read* chunk size (how many rows
    `_read_batch` scans per poll) purely to avoid a poll round-trip per row when many events
    land at once — every matching event is still sent on the wire as its own `"event"` frame,
    never grouped into an `"events"` batch, matching PRD §26.2's live shape exactly.
    """
    config = state.config.api
    seen_finding_ids: list[str] = []
    sent_verdict = False
    reported_status: str | None = None
    live_index = 0

    while not session.done.is_set():
        matched, next_from_seq, _exhausted = _read_batch(
            store, session.run_id, from_seq, config.ws_backlog_batch_size, session.types
        )
        any_pause = False
        for event in matched:
            live_index += 1
            if session.sampling_n is not None and live_index % session.sampling_n != 0:
                continue  # sampled out — the client was told via the "status" sampling frame
            paused = await _await_flow_control(session, config.ws_flow_control_max_unacked)
            any_pause = any_pause or paused
            if session.done.is_set():
                return
            await websocket.send_json({"type": "event", "event": _event_json(event)})
            session.last_sent_seq = event.seq
        from_seq = next_from_seq
        session.consecutive_pauses = session.consecutive_pauses + 1 if any_pause else 0
        await _maybe_update_sampling(session, websocket)

        record = store.get_run(session.run_id)
        current_status = record.status if record is not None else None
        if current_status != reported_status:
            reported_status = current_status
            if current_status is not None:
                await websocket.send_json({"type": "status", "status": current_status})

        for finding in store.list_findings(session.run_id):
            if finding.finding_id in seen_finding_ids:
                continue
            seen_finding_ids.append(finding.finding_id)
            await websocket.send_json(
                {"type": "finding", "finding": _project_finding(finding).model_dump(mode="json")}
            )

        if not sent_verdict:
            scorecard = store.get_scorecard(session.run_id)
            if scorecard is not None:
                await websocket.send_json({"type": "verdict", "verdict": dict(scorecard.payload)})
                sent_verdict = True

        if not matched:
            try:
                await asyncio.wait_for(session.done.wait(), timeout=config.ws_poll_interval_s)
            except TimeoutError:
                pass


async def _sender(websocket: WebSocket, session: _Session, store: Store, state: ApiState) -> None:
    """Stream backlog then live events/findings/verdict/status for a subscribed session."""
    await session.subscribed.wait()
    if session.done.is_set():
        return
    session.acked_through = session.from_seq - 1
    resume_from = await _send_backlog(websocket, session, store, state.config.api)
    if session.done.is_set():
        return
    await _send_live(websocket, session, store, state, resume_from)


@router.websocket("/ws/runs/{run_id}")
async def stream_run(
    websocket: WebSocket,
    run_id: str,
    state: Annotated[ApiState, Depends(get_state_ws)],
    store: Annotated[Store, Depends(get_store_async)],
) -> None:
    """PRD §26.2: live event stream — connection lifecycle and the two rejecting close codes.

    Rejects before `accept()`: `4004` when `run_id` names no run, `4013` when this run
    already has `config.ws_max_connections_per_run` open connections — PRD §26.2's close-code
    table is a handshake-rejection vocabulary for these two, not a post-accept one. Every
    other ending (heartbeat timeout, client `unsubscribe`, client disconnect) closes a
    successfully `accept()`-ed connection with `1000`: PRD §26.2 defines no dedicated code for
    those, and a connection the server itself chose to end is a normal closure from its side.
    """
    if store.get_run(run_id) is None:
        await websocket.close(code=_CLOSE_RUN_NOT_FOUND)
        return
    if not state.ws_connections.try_connect(run_id, state.config.api.ws_max_connections_per_run):
        await websocket.close(code=_CLOSE_TOO_MANY_CONNECTIONS)
        return

    try:
        await websocket.accept()
        record = store.get_run(run_id)
        # seq is a strictly increasing, gap-free counter within a run (PRD Sec9: append-only,
        # PRIMARY KEY (run_id, seq)) — the highest currently-stored seq is therefore exactly
        # `event_count - 1`, with no separate "max seq" query needed.
        current_seq = store.event_count(run_id) - 1
        await websocket.send_json(
            {
                "type": "hello",
                "run_id": run_id,
                "status": record.status if record is not None else "unknown",
                "current_seq": current_seq,
                "schema_version": SCHEMA_VERSION,
            }
        )

        session = _Session(run_id=run_id)
        receiver_task: asyncio.Task[None] = asyncio.ensure_future(
            _receiver(websocket, session, state.config.api)
        )
        sender_task: asyncio.Task[None] = asyncio.ensure_future(
            _sender(websocket, session, store, state)
        )
        try:
            await asyncio.wait({receiver_task, sender_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            session.done.set()
            for task in (receiver_task, sender_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receiver_task, sender_task, return_exceptions=True)

        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=_CLOSE_NORMAL)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - no REST-style global handler covers WS routes
        # `errors.py`'s handlers are ASGI-HTTP-scoped; this logs with its real traceback
        # server-side, matching `errors.py::_handle_unexpected`'s own "never a swallowed
        # exception" rule, applied here by hand since there is no framework hook to register
        # this on for a websocket route.
        _log.exception("ws %s: unhandled error", run_id)
    finally:
        state.ws_connections.disconnect(run_id)
