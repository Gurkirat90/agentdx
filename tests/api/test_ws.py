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


def test_flow_control_pauses_and_resumes_on_ack(
    api_config: AgentDXConfig, db_path: Path, sealed_run: tuple[str, tuple]
) -> None:
    """PRD §26.2 flow control: `ack` resumes a sender paused on too many unacked events.

    OP-2 audit finding #5 (`op2-audit-p14.md`): `_await_flow_control`/`_handle_ack`/
    `_maybe_update_sampling` had zero test coverage before this — no bug demonstrated, a pure
    coverage gap the audit flagged honestly. `ws_backlog_batch_size=5` /
    `ws_flow_control_max_unacked=4` (the same `dataclasses.replace(api_config,
    api=api_config.api.with_overrides(...))` override pattern
    `test_too_many_connections_closes_4013`/`test_heartbeat_timeout_closes_1000` already use)
    make the second backlog batch (seqs 5-9) provably block: after the first batch sends
    seqs 0-4, `last_sent_seq(4) - acked_through(-1) = 5 > 4`, so `_send_backlog` is genuinely
    suspended in `ack_event.wait()` when the `ack` below is sent — not merely "would
    eventually have arrived regardless" — and only that `ack` wakes it to send the second
    batch. The test never has more than one unread frame in flight at a time (each batch is
    received before the next `ack` is sent), so it makes no assumption about the WS test
    transport's buffering.
    """
    run_id, _events = sealed_run
    flow_config = dataclasses.replace(
        api_config,
        api=api_config.api.with_overrides(ws_backlog_batch_size=5, ws_flow_control_max_unacked=4),
    )
    app = create_app(config=flow_config, store_path=db_path)
    with TestClient(app) as client, client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "subscribe", "from_seq": 0})

        first_batch = ws.receive_json()
        assert first_batch["type"] == "events"
        assert [e["seq"] for e in first_batch["events"]] == list(range(5))

        # At this point the sender has attempted to read the second batch and is blocked in
        # `_await_flow_control` — nothing further can be on the wire until this ack unblocks
        # it (proven by the assert right after: the second batch could not have been sent,
        # let alone already read, before `through_seq=4` raises `acked_through` to 4).
        ws.send_json({"type": "ack", "through_seq": 4})

        second_batch = ws.receive_json()
        assert second_batch["type"] == "events"
        assert [e["seq"] for e in second_batch["events"]] == list(range(5, 10))

        ws.send_json({"type": "unsubscribe"})


def test_three_consecutive_pauses_escalate_to_sampled_mode(
    api_config: AgentDXConfig, db_path: Path
) -> None:
    """PRD §26.2 "switches to sampled mode": three consecutive live-tailing pauses trigger it.

    OP-2 audit finding #5, the other half of the same coverage gap
    `test_flow_control_pauses_and_resumes_on_ack` closes — this one targets `_send_live`'s
    `consecutive_pauses`/`_maybe_update_sampling` path specifically, since `ws.py`'s own
    module docstring says a *backlog* pause never counts ("backlog sending already blocks on
    flow control by design — a pause there is not evidence of trouble"); only a pause while
    tailing *live* does. `ws_backlog_batch_size=1` makes `_read_batch` return at most one
    event per read — both for the backlog drain and, per `_send_live`'s own docstring, as its
    internal live read-chunk size too — so each of the three events below is discovered and
    paused on in its own separate `_send_live` loop iteration, never grouped into one.

    All three live events are written to the store in one call, before any of them is acked,
    so no iteration can ever observe an empty read in between them — which would silently
    reset `consecutive_pauses` back to 0 — purely because of scheduling luck. The sequence
    below is deterministic, not timing-dependent: every wait blocks with no I/O in flight
    (the flow-control check runs before `send_json`, never after), and every send is drained
    by exactly one `receive_json` before the next `ack` goes out, so nothing is ever left
    unread on the wire for the transport's buffering to matter.
    """
    events = build_log(spans=1, sealed=False)  # unsealed: status stays "running" throughout
    run_id = events[0].run_id
    chained = chain(events)

    store = Store.open(db_path, config=api_config.store)
    try:
        record = RunRecord(**run_record_for(events))  # type: ignore[arg-type]
        store.create_run(record)
        store.append(chained[:1])  # just run_start — the entire initial backlog
    finally:
        store.close()

    sampling_config = dataclasses.replace(
        api_config,
        api=api_config.api.with_overrides(
            ws_backlog_batch_size=1, ws_flow_control_max_unacked=0, ws_poll_interval_s=0.05
        ),
    )
    app = create_app(config=sampling_config, store_path=db_path)
    with TestClient(app) as client, client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "subscribe", "from_seq": 0})

        backlog = ws.receive_json()
        assert backlog["type"] == "events"
        assert [e["seq"] for e in backlog["events"]] == [0]

        # `_send_live`'s first iteration cannot find any live event yet (none appended below
        # this line yet) — it reports the run's status before ever attempting a flow-control
        # wait, deterministically, not by timing luck.
        running_status = ws.receive_json()
        assert running_status == {"type": "status", "status": "running"}

        store2 = Store.open(db_path, config=api_config.store)
        try:
            store2.append(chained[1:4])  # three more real events, all before any ack
        finally:
            store2.close()

        for expected_seq, through_seq in ((1, 0), (2, 1), (3, 2)):
            # Each event is discovered already paused (`max_unacked=0` and `acked_through`
            # only ever advances to the previous event's own seq) — the ack below is what
            # lets `_send_live` actually send it.
            ws.send_json({"type": "ack", "through_seq": through_seq})
            frame = ws.receive_json()
            assert frame["type"] == "event"
            assert frame["event"]["seq"] == expected_seq

        sampling_status = ws.receive_json()
        assert sampling_status == {"type": "status", "sampling": 2}

        ws.send_json({"type": "unsubscribe"})
