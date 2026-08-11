"""State snapshots — an *optimisation* for §20.4 reconstruction, never the source of truth.

PRD §20.4 reconstructs the state visible at an arbitrary virtual timestamp by folding
`state_write` events. Done naively that is O(run length) per scrub, which makes the timeline
scrubber unusable on a 50 000-event run. Snapshots every N events bound it at O(N).

**The property that makes this safe.** A snapshot must always be reproducible by replaying
the log. This module is arranged so that the same fold produces both: `StateFold` is the
only place a `state_write` is ever interpreted, `SnapshottingStore` drives it during
ingestion, and `rebuild_snapshots` drives it over a stored log. If the two could diverge,
a corrupted or stale snapshot would silently become an alternative history — and because
reconstruction reads the snapshot first, that history would be the one the UI showed. So
the equivalence is not an assumption; `tests/unit/store/test_snapshot_equivalence.py`
asserts `state_at == state_by_replay` at random timestamps *and* that the rows written
during ingestion are byte-identical to the rows rebuilt from the sealed log.

**Deleting every snapshot is always safe.** `state_at` with no snapshots is exactly
`state_by_replay`. That is the operational form of "the log is the source of truth": if the
snapshots are ever in doubt, `DELETE FROM state_snapshots` costs latency and nothing else.
Note that this is a table the append-only triggers deliberately do not cover — snapshots
are derived data, and derived data is regenerable by definition.

**Bodies follow capture_bodies (I8).** With `capture_bodies=False` a `state_write` payload
carries `value_hash` and no `value`, so the reconstructed state carries the identity of the
value and its writer but not its content. PRD §20.4 states that this is honest and still
sufficient for conflict analysis; this module never invents a body it was not given.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentdx.config import StoreConfig
from agentdx.events.canonical import encode_value
from agentdx.events.schema import Event, EventType, PayloadValue
from agentdx.events.writer import ChainedEvent
from agentdx.store.sqlite import Store, StoreError

_STATE_KEY: Final = "key"
_VALUE_HASH: Final = "value_hash"
_VALUE: Final = "value"


@dataclass(frozen=True, slots=True)
class ValueRef:
    """What the state table holds for one key at a point in virtual time (PRD §20.4).

    Guarantees: `value_hash`, `writer` and `seq` are always present — they are the identity
    of the value and the provenance of the write, which is what conflict analysis needs.
    `body` is present only when the run captured bodies (I8); None means "not captured",
    never "empty".
    """

    value_hash: str
    writer: str | None
    seq: int
    body: str | None = None

    def as_mapping(self) -> dict[str, PayloadValue]:
        """Return the canonical-JSON form stored in `state_snapshots.state`.

        Guarantees: the key set is fixed and does not vary with whether a body was
        captured, so two snapshots of the same fold encode to identical bytes regardless of
        which of them was built during ingestion and which by replay.
        """
        return {
            "value_hash": self.value_hash,
            "writer": self.writer,
            "seq": self.seq,
            "body": self.body,
        }


State = Mapping[str, ValueRef]
"""The reconstructed state: key -> the reference written most recently at or before a seq."""


class StateFold:
    """The single interpretation of `state_write` in this codebase.

    Both the ingestion path and the replay path drive this class, which is what makes a
    stored snapshot and a replayed one provably the same object rather than two
    implementations that happen to agree today.

    Guarantees: order-dependent and deterministic — folding the same events in the same
    order always produces the same state. Ignores every event type except `state_write`;
    `state_read` does not change state and is deliberately not consulted.
    """

    def __init__(self, initial: State | None = None) -> None:
        """Start a fold, optionally from a snapshot's state."""
        self._state: dict[str, ValueRef] = dict(initial or {})

    def apply(self, event: Event) -> None:
        """Fold one event into the state.

        Guarantees: a non-`state_write` event is a no-op. A `state_write` replaces the entry
        for its key wholesale — last write wins, which is what the log records and what the
        race detector later reasons about.

        Raises:
            StoreError: `E-STORE-014` a `state_write` payload lacks `key` or `value_hash`.
                The event contract requires both, so this can only fire on a log that
                bypassed validation.
        """
        if event.type is not EventType.STATE_WRITE:
            return
        key = event.payload.get(_STATE_KEY)
        value_hash = event.payload.get(_VALUE_HASH)
        if not isinstance(key, str) or not isinstance(value_hash, str):
            raise StoreError(
                "E-STORE-014",
                f"state_write at seq={event.seq} lacks a string {_STATE_KEY!r} or "
                f"{_VALUE_HASH!r}; the event contract requires both (PRD §9.5)",
            )
        body = event.payload.get(_VALUE)
        self._state[key] = ValueRef(
            value_hash=value_hash,
            writer=event.agent_id,
            seq=event.seq,
            body=body if isinstance(body, str) else None,
        )

    def state(self) -> dict[str, ValueRef]:
        """Return a copy of the current state, so a caller cannot mutate the fold."""
        return dict(self._state)

    def encode(self) -> str:
        """Return the canonical JSON stored in `state_snapshots.state`.

        Guarantees: uses `canonical.encode_value`, so key order and escaping match every
        other serialised structure in the system and two equal states encode to identical
        bytes. That is what lets the rebuild test compare stored and replayed snapshots as
        strings rather than as parsed objects.
        """
        return encode_value({k: v.as_mapping() for k, v in self._state.items()})


class SnapshottingStore(Store):
    """A `Store` that materialises a state snapshot every `snapshot_interval_events`.

    Guarantees: a snapshot row is written inside the *same transaction* as the batch whose
    events it summarises, so a crash can never leave a snapshot describing events that are
    not in the log. The converse — events with no snapshot — is always fine, because a
    missing snapshot only costs reconstruction time.

    Use this class wherever a run is being recorded. Plain `Store` is for readers (the API
    process, PRD §24.2) and for bulk import, which rebuilds snapshots in one pass instead.
    """

    def __init__(self, conn: sqlite3.Connection, config: StoreConfig, path: Path) -> None:
        """Wrap a connection and start with an empty per-run fold. Prefer `Store.open`."""
        super().__init__(conn, config, path)
        self._folds: dict[str, StateFold] = {}

    def _on_batch_persisted(self, run_id: str, batch: Sequence[ChainedEvent]) -> None:
        """Fold the batch and write any snapshot rows the interval calls for.

        Runs inside `append`'s transaction. Guarantees: snapshots are placed at exactly the
        seq values `rebuild_snapshots` chooses, so the two paths agree by construction
        rather than by coincidence.
        """
        interval = self.config.snapshot_interval_events
        fold = self._folds.setdefault(run_id, StateFold())
        rows: list[tuple[str, int, str]] = []
        for chained in batch:
            fold.apply(chained.event)
            if is_snapshot_seq(chained.event.seq, interval):
                rows.append((run_id, chained.event.seq, fold.encode()))
        if rows:
            self.connection.executemany(
                "INSERT INTO state_snapshots (run_id, seq, state) VALUES (?,?,?) "
                "ON CONFLICT(run_id, seq) DO UPDATE SET state = excluded.state",
                rows,
            )


def is_snapshot_seq(seq: int, interval: int) -> bool:
    """Return True iff a snapshot is materialised after the event at `seq`.

    PRD §20.4 says "every 500th event", so with the default interval snapshots land after
    seq 499, 999, 1499 … — that is, after each complete group of `interval` events.

    Guarantees: the single definition of snapshot placement. Both the ingestion path and
    the rebuild path call it, so they cannot choose different seq values.
    """
    return interval > 0 and (seq + 1) % interval == 0


def rebuild_snapshots(events: Iterable[Event], interval: int) -> dict[int, str]:
    """Return the snapshot rows a log implies: `seq -> canonical state JSON`.

    This is the "reproducible by replaying events" property made executable. It shares
    `StateFold` and `is_snapshot_seq` with the ingestion path, so a difference between
    stored and rebuilt rows can only mean the stored rows are stale or corrupt — which is
    exactly what the test is for.

    Guarantees: pure. Reads no database, no clock and no configuration.
    """
    fold = StateFold()
    out: dict[int, str] = {}
    for event in events:
        fold.apply(event)
        if is_snapshot_seq(event.seq, interval):
            out[event.seq] = fold.encode()
    return out


def stored_snapshots(store: Store, run_id: str) -> dict[int, str]:
    """Return the snapshot rows a run currently has: `seq -> canonical state JSON`."""
    rows = store.connection.execute(
        "SELECT seq, state FROM state_snapshots WHERE run_id = ? ORDER BY seq", (run_id,)
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def write_snapshots(store: Store, run_id: str, rows: Mapping[int, str]) -> None:
    """Replace a run's snapshot rows with `rows`, in one transaction.

    Used by bundle import, which has the whole log at once and so rebuilds in a single pass
    rather than pretending to ingest.

    Guarantees: `state_snapshots` is derived data with no append-only trigger, so replacing
    it is legitimate in a way that replacing an event never is. Uses `Store.transaction()`
    rather than its own `BEGIN`, so it joins an import's transaction instead of failing
    inside one.
    """
    conn = store.connection
    with store.transaction():
        conn.execute("DELETE FROM state_snapshots WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO state_snapshots (run_id, seq, state) VALUES (?,?,?)",
            [(run_id, seq, state) for seq, state in sorted(rows.items())],
        )


def last_seq_at_or_before(store: Store, run_id: str, virtual_ts_ms: int) -> int | None:
    """Return the highest `seq` whose `virtual_ts_ms` is at or before `virtual_ts_ms`.

    Guarantees: uses `idx_events_vts`, and breaks ties by `seq` — several events commonly
    share a virtual timestamp because the virtual clock only advances when the scheduler
    says so (PRD §10.3), and taking the highest seq is what "the state as of that instant"
    means.
    """
    row = store.connection.execute(
        "SELECT MAX(seq) FROM events WHERE run_id = ? AND virtual_ts_ms <= ?",
        (run_id, virtual_ts_ms),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def nearest_snapshot(store: Store, run_id: str, target_seq: int) -> tuple[int, State] | None:
    """Return the latest snapshot at or before `target_seq`, or None when there is none.

    Guarantees: decoding a snapshot never invents a field — a row written by an older build
    that lacks `body` decodes with `body=None`, which is indistinguishable from "not
    captured" and therefore correct.
    """
    row = store.connection.execute(
        "SELECT seq, state FROM state_snapshots WHERE run_id = ? AND seq <= ? "
        "ORDER BY seq DESC LIMIT 1",
        (run_id, target_seq),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), _decode_state(str(row[1]))


def state_at(store: Store, run_id: str, virtual_ts_ms: int) -> dict[str, ValueRef]:
    """Return the state visible at a virtual timestamp, using snapshots (PRD §20.4).

    Guarantees: identical to `state_by_replay` for every input — snapshots change how long
    this takes, never what it returns. Reconstruction touches at most
    `snapshot_interval_events` events regardless of run length.

    Args:
        store: An open store containing the run.
        run_id: The run to reconstruct.
        virtual_ts_ms: The virtual timestamp to reconstruct at, inclusive.

    Returns:
        Key -> `ValueRef` for every key written at or before that instant. Empty when the
        timestamp precedes the first event.
    """
    target = last_seq_at_or_before(store, run_id, virtual_ts_ms)
    if target is None:
        return {}
    snapshot = nearest_snapshot(store, run_id, target)
    if snapshot is None:
        fold = StateFold()
        start = 0
    else:
        snapshot_seq, state = snapshot
        fold = StateFold(state)
        start = snapshot_seq + 1
    for event in store.read_events(run_id, from_seq=start, to_seq=target):
        fold.apply(event)
    return fold.state()


def state_by_replay(store: Store, run_id: str, virtual_ts_ms: int) -> dict[str, ValueRef]:
    """Return the state visible at a virtual timestamp by folding the log from scratch.

    This is the definition; `state_at` is the accelerated implementation of it. Kept as a
    first-class public function rather than a test helper, because "the log is the source
    of truth" is only a real property if the from-scratch path is something the product can
    actually run — `agentdx doctor` uses it to check a suspect database.

    Guarantees: consults no snapshot. O(events up to the timestamp).
    """
    target = last_seq_at_or_before(store, run_id, virtual_ts_ms)
    if target is None:
        return {}
    fold = StateFold()
    for event in store.read_events(run_id, to_seq=target):
        fold.apply(event)
    return fold.state()


def iter_state_writes(events: Iterable[Event]) -> Iterator[Event]:
    """Yield only the `state_write` events of a log, in order.

    Guarantees: the filter used by every state consumer, so "which events change state" has
    one answer in this codebase.
    """
    for event in events:
        if event.type is EventType.STATE_WRITE:
            yield event


def _decode_state(text: str) -> dict[str, ValueRef]:
    """Return the `ValueRef` map a stored snapshot encodes.

    Raises:
        StoreError: `E-STORE-015` the snapshot row is not the shape this module writes,
            which means it was written by something else. Snapshots are regenerable, so the
            recovery is to delete them and let reconstruction fall back to a full replay.
    """
    parsed: object = json.loads(text)
    if not isinstance(parsed, Mapping):
        raise StoreError("E-STORE-015", "a state_snapshots row does not hold a JSON object")
    out: dict[str, ValueRef] = {}
    for key, value in parsed.items():
        if not isinstance(value, Mapping):
            raise StoreError(
                "E-STORE-015",
                f"snapshot entry {key!r} is not an object; delete the "
                f"snapshots for this run and reconstruction will replay the log instead",
            )
        value_hash = value.get("value_hash")
        seq = value.get("seq")
        if not isinstance(value_hash, str) or not isinstance(seq, int):
            raise StoreError(
                "E-STORE-015", f"snapshot entry {key!r} lacks a string value_hash or an integer seq"
            )
        writer = value.get("writer")
        body = value.get("body")
        out[str(key)] = ValueRef(
            value_hash=value_hash,
            writer=writer if isinstance(writer, str) else None,
            seq=seq,
            body=body if isinstance(body, str) else None,
        )
    return out


__all__ = [
    "SnapshottingStore",
    "State",
    "StateFold",
    "ValueRef",
    "is_snapshot_seq",
    "iter_state_writes",
    "last_seq_at_or_before",
    "nearest_snapshot",
    "rebuild_snapshots",
    "state_at",
    "state_by_replay",
    "stored_snapshots",
    "write_snapshots",
]
