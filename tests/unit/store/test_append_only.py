"""Invariant I2, enforced by the database rather than by discipline (design constraint 1).

The point of these tests is not that `store/sqlite.py` never issues an UPDATE — that would
only prove something about today's code. The point is that an UPDATE or a DELETE against
`events` **fails**, from any connection, whoever issues it. So every test here goes around
the `Store` API and talks to `sqlite3` directly, which is exactly what a later prompt, an
`agentdx` shell session, or a curious user would do.
"""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.events.schema import Event
from agentdx.store.migrations import trigger_names
from agentdx.store.sqlite import APPEND_ONLY_MESSAGE, Store, StoreError
from tests.unit.store.conftest import chain, populate
from tests.unit.store.factories import build_log


def test_update_on_events_raises(populated: tuple[Store, str, tuple[Event, ...]]) -> None:
    """An UPDATE against `events` aborts with the trigger's message."""
    store, run_id, _ = populated
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        store.connection.execute(
            "UPDATE events SET payload = ? WHERE run_id = ? AND seq = 1",
            ('{"tampered":true}', run_id),
        )
    assert APPEND_ONLY_MESSAGE in str(excinfo.value)


def test_delete_on_events_raises(populated: tuple[Store, str, tuple[Event, ...]]) -> None:
    """A DELETE against `events` aborts with the trigger's message."""
    store, run_id, _ = populated
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        store.connection.execute("DELETE FROM events WHERE run_id = ? AND seq = 1", (run_id,))
    assert APPEND_ONLY_MESSAGE in str(excinfo.value)


def test_update_from_an_unrelated_connection_raises(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """The refusal belongs to the database, not to the `Store` object holding it open.

    A second process — the API reader of PRD §24.2, or `sqlite3` on the command line — is
    subject to the same trigger. If this test ever passes only for the owning connection,
    I2 has become application discipline again.
    """
    store, run_id, _ = populated
    other = sqlite3.connect(str(store.path))
    try:
        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            other.execute("UPDATE events SET seq = seq + 1000 WHERE run_id = ?", (run_id,))
        assert APPEND_ONLY_MESSAGE in str(excinfo.value)
    finally:
        other.close()


def test_no_rows_change_after_a_refused_update(
    populated: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """A refused UPDATE leaves the log byte-identical, not merely mostly unchanged."""
    store, run_id, _ = populated
    before = [c.this_hash for c in store.read_chained(run_id)]
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("UPDATE events SET wall_ts_ms = 0 WHERE run_id = ?", (run_id,))
    assert [c.this_hash for c in store.read_chained(run_id)] == before
    assert store.verify_chain(run_id) is None


def test_both_triggers_exist_after_open(store: Store) -> None:
    """Opening a store leaves both append-only triggers in place.

    Asserted against `trigger_names()` — the same tuple the migration runner verifies after
    every migration — so this test cannot pass against a trigger the runner does not check.
    """
    present = {
        str(row[0])
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert set(trigger_names()) <= present


def test_seal_refuses_further_appends(store: Store) -> None:
    """After `seal`, appending to that run is `E-STORE-005` rather than a silent write."""
    events = build_log(spans=2)
    run_id = populate(store, events)
    with pytest.raises(StoreError) as excinfo:
        store.append(chain(events)[:1])
    assert excinfo.value.code == "E-STORE-005"
    assert run_id in str(excinfo.value)


def test_duplicate_seq_is_refused_by_the_primary_key(store: Store) -> None:
    """Re-appending an already stored `(run_id, seq)` is refused by the primary key.

    This is the other half of append-only: the triggers stop history being rewritten in
    place, and the primary key stops it being rewritten by re-insertion.
    """
    events = build_log(spans=2)
    populate(store, events, seal=False)
    with pytest.raises(sqlite3.IntegrityError):
        store.append(chain(events)[:2])


def test_append_to_an_unknown_run_is_refused(store: Store) -> None:
    """Events cannot be appended before their run row exists (`E-STORE-004`).

    Requiring provenance first is what makes an orphan event set impossible, and it is what
    lets `append` tell "sealed" apart from "never existed".
    """
    events = build_log(spans=1)
    with pytest.raises(StoreError) as excinfo:
        store.append(chain(events)[:1])
    assert excinfo.value.code == "E-STORE-004"


def test_state_snapshots_are_deliberately_not_append_only(
    tmp_path: Path, config: StoreConfig
) -> None:
    """Derived data is deletable; that is the operational form of "the log is the truth".

    If snapshots are ever in doubt they can be dropped, and reconstruction falls back to a
    full replay of the log with the same result. A trigger here would turn a regenerable
    cache into a second immutable history.
    """
    opened = Store.open(tmp_path / "agentdx.db", config=config)
    try:
        opened.connection.execute("DELETE FROM state_snapshots")
    finally:
        opened.close()


def test_a_failed_checkpoint_warns_rather_than_being_swallowed(
    tmp_path: Path, config: StoreConfig
) -> None:
    """OP-2 F5: AGENTS.md §4 — "never a swallowed exception".

    `close()` used to catch `sqlite3.Error` from the WAL checkpoint and `pass`. The
    rationale was sound (a failed checkpoint is not a correctness problem) but the silence
    was not: a `-wal` file left beside the database means copying only the `.db` loses the
    most recent events, and PRD §27.5 tells users that copying the data dir *is* the
    backup. The failure is now surfaced as a warning and the close still succeeds.
    """
    store = Store.open(tmp_path / "agentdx.db", config=config)
    # Close the underlying connection out from under the store: the checkpoint then raises
    # sqlite3.ProgrammingError, which is the realistic shape of a checkpoint that cannot run.
    store.connection.close()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store.close()

    assert any("E-STORE-019" in str(w.message) for w in caught), "the failure was swallowed"
    assert any("data directory" in str(w.message) or "-wal" in str(w.message) for w in caught)
