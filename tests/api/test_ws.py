"""`WS /ws/runs/{id}` (PRD §26.2): protocol, backlog/live boundary, reconnect, flow control."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentdx.api.app import create_app
from agentdx.config import AgentDXConfig
from agentdx.store.sqlite import RunRecord, Store
from tests.unit.store.conftest import chain, populate
from tests.unit.store.factories import build_log, run_record_for


def _drain_backlog(ws) -> list[dict]:  # noqa: ANN001
    """Receive `subscribe`'s `"events"` batches until the first non-`"events"` message."""
    batches = []
    while True:
        msg = ws.receive_json()
        if msg["type"] != "events":
            return batches, msg  # type: ignore[return-value]
        batches.append(msg)


def test_connect_to_unknown_run_closes_4004(client: TestClient) -> None:
    """PRD §26.2 close code 4004: the run does not exist, rejected before `accept()`."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/runs/r_missing"):
            pass
    assert excinfo.value.code == 4004


def test_hello_then_backlog_then_status(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """Connect, `subscribe`, and get `hello` -> backlog `events` -> `status` (PRD §26.2 order)."""
    run_id, events = sealed_run
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["run_id"] == run_id
        assert hello["status"] == "complete"
        assert hello["current_seq"] == len(events) - 1

        ws.send_json({"type": "subscribe", "from_seq": 0})
        batches, next_msg = _drain_backlog(ws)
        assert batches, "expected at least one backlog events batch"
        all_seqs = [e["seq"] for batch in batches for e in batch["events"]]
        assert all_seqs == list(range(len(events)))
        assert next_msg["type"] == "status"
        assert next_msg["status"] == "complete"

        ws.send_json({"type": "unsubscribe"})


def test_subscribe_filters_by_type(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """`filters.types` restricts the backlog to the named `EventType`s only."""
    run_id, _events = sealed_run
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        ws.receive_json()  # hello
        ws.send_json(
            {"type": "subscribe", "from_seq": 0, "filters": {"types": ["run_start", "run_end"]}}
        )
        batches, _next_msg = _drain_backlog(ws)
        seen_types = {e["type"] for batch in batches for e in batch["events"]}
        assert seen_types == {"run_start", "run_end"}
        ws.send_json({"type": "unsubscribe"})


def test_ping_gets_pong(client: TestClient, sealed_run: tuple[str, tuple]) -> None:
    """A bare `ping` (no subscribe needed) gets a `pong` — the receiver, not the sender, replies."""
    run_id, _events = sealed_run
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"type": "unsubscribe"})


def test_unknown_message_type_gets_an_error_frame(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """An unrecognised `type` is a PRD §26.2-shaped `error` frame, not a dropped connection."""
    run_id, _events = sealed_run
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "not_a_real_type"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "E-WS-002"
        ws.send_json({"type": "unsubscribe"})


def test_too_many_connections_closes_4013(
    api_config: AgentDXConfig, db_path: Path, sealed_run: tuple[str, tuple]
) -> None:
    """PRD §26.2 close code 4013: a run's connection limit is enforced per run.

    Pre-fills `state.ws_connections` to the limit rather than opening two genuinely
    concurrent WS connections through `TestClient`: Starlette's `TestClient` drives every
    WebSocket connection through one shared background portal, and a second connection
    attempted while a first is still open corrupts that portal's ability to cleanly close the
    first afterwards (confirmed empirically — a minimal two-connection repro raises
    `concurrent.futures.CancelledError` from the *first* connection's own `__exit__`, purely
    a `TestClient` limitation, not a `ws.py` behaviour: the same repro run directly against
    `uvicorn`/plain `asyncio`, outside `TestClient`, works cleanly). Pre-filling the guard
    exercises the exact same route-handler check (`state.ws_connections.try_connect`, `ws.py`
    `stream_run`) with a single connection, sidestepping the portal issue entirely.
    """
    run_id, _events = sealed_run
    limited_config = dataclasses.replace(
        api_config, api=api_config.api.with_overrides(ws_max_connections_per_run=1)
    )
    app = create_app(config=limited_config, store_path=db_path)
    with TestClient(app) as client:
        state = app.state.agentdx
        assert state.ws_connections.try_connect(run_id, limit=1)  # occupy the one slot
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/ws/runs/{run_id}"):
                pass
        assert excinfo.value.code == 4013


def test_reconnect_with_from_seq_has_no_duplicate_and_no_gap(
    client: TestClient, sealed_run: tuple[str, tuple]
) -> None:
    """A reconnect with `from_seq = last_seen + 1` never repeats or skips a seq.

    PRD §26.2 "Reconnect": "no event is ever delivered twice".
    """
    run_id, events = sealed_run
    total = len(events)
    split = total // 2

    first_seen: list[int] = []
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "subscribe", "from_seq": 0})
        while len(first_seen) < split:
            msg = ws.receive_json()
            if msg["type"] == "events":
                first_seen.extend(e["seq"] for e in msg["events"])
        ws.send_json({"type": "unsubscribe"})

    # Trim to exactly `split` in case the last batch overshot; a real client that only
    # processed the first `split` events would ack accordingly and resume right after them.
    first_seen = first_seen[:split]
    resume_from = first_seen[-1] + 1

    second_seen: list[int] = []
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        hello = ws.receive_json()
        assert hello["current_seq"] == total - 1
        ws.send_json({"type": "subscribe", "from_seq": resume_from})
        while len(second_seen) < total - split:
            msg = ws.receive_json()
            if msg["type"] == "events":
                second_seen.extend(e["seq"] for e in msg["events"])
        ws.send_json({"type": "unsubscribe"})

    combined = first_seen + second_seen[: total - split]
    assert combined == list(range(total))
    # No duplicate: the two segments are disjoint.
    assert set(first_seen) & set(second_seen[: total - split]) == set()


def test_live_tail_sees_events_appended_after_subscribe(
    api_config: AgentDXConfig, db_path: Path
) -> None:
    """A run still `running` when the WS connects streams new events live, one at a time."""
    # Compute the hash chain over the *whole* eventual log up front, then append it to the
    # store in two batches — the first before the WS connects (what backlog sees), the
    # second after (what live-tailing must pick up) — so every appended event is a real,
    # chain-valid continuation of the run, not a hand-patched standalone one.
    events = build_log(spans=2, sealed=False)  # unsealed: status stays "running"
    split = len(events) // 2
    run_id = events[0].run_id
    chained = chain(events)

    store = Store.open(db_path, config=api_config.store)
    try:
        record = RunRecord(**run_record_for(events))  # type: ignore[arg-type]
        store.create_run(record)
        store.append(chained[:split])
    finally:
        store.close()

    app = create_app(config=api_config, store_path=db_path)
    with TestClient(app) as client, client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        hello = ws.receive_json()
        assert hello["status"] == "running"
        ws.send_json({"type": "subscribe", "from_seq": 0})
        batches, next_msg = _drain_backlog(ws)
        backlog_seqs = [e["seq"] for batch in batches for e in batch["events"]]
        assert backlog_seqs == list(range(split))
        # Whatever arrived right after the backlog (a poll-cycle "status", most likely) is
        # not itself the live event we are about to append — queue it and keep looking.
        pending = [next_msg]

        store2 = Store.open(db_path, config=api_config.store)
        try:
            store2.append(chained[split:])
        finally:
            store2.close()

        live_seqs: list[int] = []
        for _ in range(200):
            msg = pending.pop(0) if pending else ws.receive_json()
            if msg["type"] == "event":
                live_seqs.append(msg["event"]["seq"])
                if len(live_seqs) == len(events) - split:
                    break
        assert live_seqs == list(range(split, len(events)))
        ws.send_json({"type": "unsubscribe"})


def test_heartbeat_timeout_closes_1000(api_config: AgentDXConfig, db_path: Path) -> None:
    """PRD §26.2: "server closes after 45s of silence" — proven with a short configured timeout."""
    store = Store.open(db_path, config=api_config.store)
    try:
        events = build_log(spans=1)
        run_id = populate(store, events)
    finally:
        store.close()

    fast_timeout_config = dataclasses.replace(
        api_config,
        api=api_config.api.with_overrides(ws_heartbeat_timeout_s=0.3, ws_heartbeat_interval_s=0.1),
    )
    app = create_app(config=fast_timeout_config, store_path=db_path)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
                ws.receive_json()  # hello
                # Never send another message — the receiver's read times out.
                ws.receive_json()
        assert excinfo.value.code == 1000
