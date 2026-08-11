"""Stored rows must round-trip to the events that produced their hashes.

This is not a convenience property. `this_hash` is computed over an event's canonical
projection, so a store whose rows could not reproduce those exact bytes could not verify
its own chain — `verify_chain` would report tampering on an untouched database, or worse,
fail to report it on a tampered one.
"""

from __future__ import annotations

import pytest

from agentdx.events.canonical import canonical_bytes, canonical_log_hash
from agentdx.events.schema import Event, EventType
from agentdx.events.validators import validate_log
from agentdx.store.sqlite import Store
from tests.unit.store.conftest import populate
from tests.unit.store.factories import build_log


def test_factories_produce_valid_logs() -> None:
    """The factory's logs satisfy the event contract, sealed and unsealed.

    Asserted first, because every other store test would otherwise be exercising the store
    against input the writer would have rejected.
    """
    validate_log(build_log(spans=4))
    validate_log(build_log(spans=4, sealed=False))


def test_every_stored_event_round_trips_to_identical_canonical_bytes(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """Reading an event back produces byte-identical canonical bytes."""
    store, run_id, events = populated
    stored = tuple(store.read_events(run_id))
    assert len(stored) == len(events)
    for original, read_back in zip(stored, events, strict=True):
        assert canonical_bytes(original) == canonical_bytes(read_back)


def test_stored_log_hash_equals_the_in_memory_log_hash(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """The canonical log hash survives a trip through the database (I1)."""
    store, run_id, events = populated
    assert store.canonical_log_hash(run_id) == canonical_log_hash(events)


def test_every_non_canonical_field_also_survives(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """Volatile and identity fields round-trip too, even though they are not hashed.

    `wall_ts_ms` is excluded from the projection, so a store that dropped it would still
    pass the canonical-bytes test above. PRD §9.2 requires the field to exist for overhead
    accounting, so it is checked separately rather than assumed to be covered.
    """
    store, run_id, events = populated
    for original, read_back in zip(tuple(store.read_events(run_id)), events, strict=True):
        assert original.wall_ts_ms == read_back.wall_ts_ms
        assert original.run_id == read_back.run_id
        assert original.schema_version == read_back.schema_version


def test_schema_version_survives_the_round_trip(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """`events.schema_version` is stored per row, not reconstructed from `runs`.

    This is the column PRD §27.2's DDL omits. It is inside the canonical projection, so a
    store that recovered it from a join would produce the right hash only while the run row
    was present and correct — which it is not during a bundle import, where events land
    before the run metadata is trusted. Deviation D-12.
    """
    store, run_id, events = populated
    columns = {
        str(row[1]) for row in store.connection.execute("PRAGMA table_info(events)").fetchall()
    }
    assert "schema_version" in columns
    assert all(e.schema_version == events[0].schema_version for e in store.read_events(run_id))


def test_verify_chain_accepts_an_untouched_log(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """A stored, untampered run verifies end to end."""
    store, run_id, _ = populated
    assert store.verify_chain(run_id) is None


def test_verify_chain_locates_the_first_altered_event(store: Store) -> None:
    """Tampering that bypasses the triggers is still detected, and located.

    The triggers make this unreachable through SQL, so the test reproduces the only way it
    could actually happen — a file altered by a tool that dropped them — by dropping them,
    editing one row, and reinstating them. Detecting *where* matters: PRD §36's
    `E-BUNDLE-001` message names the event.
    """
    events = build_log(spans=3)
    run_id = populate(store, events, seal=False)
    conn = store.connection
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute(
        "UPDATE events SET virtual_ts_ms = virtual_ts_ms + 1 WHERE run_id = ? AND seq = 4",
        (run_id,),
    )
    conn.execute(
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
        "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
    )
    assert store.verify_chain(run_id) == 4


def test_reading_a_seq_window_matches_the_full_read(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """`from_seq`/`to_seq` return exactly the corresponding slice, inclusive at both ends."""
    store, run_id, events = populated
    window = tuple(store.read_events(run_id, from_seq=2, to_seq=5))
    assert [e.seq for e in window] == [2, 3, 4, 5]
    assert [canonical_bytes(e) for e in window] == [canonical_bytes(e) for e in events[2:6]]


def test_seal_records_the_computed_log_hash_not_the_chain_head(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """`runs.canonical_log_hash` holds the §10.7 log hash, not the chain head.

    They are different quantities — one is a rolling hash over canonical bytes, the other
    the tip of a per-event chain — and storing one in the other's column would make every
    downstream comparison quietly wrong.
    """
    store, run_id, events = populated
    record = store.get_run(run_id)
    assert record is not None
    assert record.canonical_log_hash == canonical_log_hash(events)
    assert record.canonical_log_hash != store.chain_head(run_id)
    assert record.event_count == len(events)


def test_seal_is_idempotent_but_refuses_a_different_head(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """Resealing with the same chain head is a no-op; a different one is refused."""
    from agentdx.store.sqlite import StoreError

    store, run_id, _ = populated
    head = store.chain_head(run_id)
    store.seal(run_id, head)
    with pytest.raises(StoreError) as excinfo:
        store.seal(run_id, "blake2b:" + "9" * 64)
    assert excinfo.value.code == "E-STORE-009"


def test_an_unsealed_run_reads_back_as_a_valid_partial_log(store: Store) -> None:
    """A run with no `run_end` is still a valid, readable, verifiable log (NFR-13).

    The in-process half of the crash guarantee. The out-of-process half — an actual SIGKILL
    — is `tests/integration/store/test_crash_partial_log.py`.
    """
    events = build_log(spans=3, sealed=False)
    run_id = populate(store, events, seal=False)
    stored = tuple(store.read_events(run_id))
    validate_log(stored)
    assert store.verify_chain(run_id) is None
    assert all(e.type is not EventType.RUN_END for e in stored)
    record = store.get_run(run_id)
    assert record is not None and record.sealed_at is None
