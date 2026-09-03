"""`Store.discard_orphan_run` (D-80, CONTEXT.md §9, ruled 2026-09-01, **C-34** §10).

A `run_id` collision against an **unsealed** row is replaced, not refused — the row holds
no analysable log (its prior attempt never reached `close_run`/`seal`), so it is deleted so
the colliding re-run can proceed. The one thing every test here cares about proving: this
narrowly, transactionally bypasses `events_no_delete`/`events_no_update` (the *only*
enforcement of I2, per `store/sqlite.py`'s own module docstring) for exactly one run's own
rows, verified to still refuse a **sealed** run and to leave the triggers fully
functional — for every other run, and for a fresh write to the same run afterward —
once it returns. This is `d78-plan.md` §7 test 4 ("a sealed run is never deleted by the
orphan path, asserted at the store/ boundary"), plus the store-level half of test 3
("an orphaned run proceeds to a real run") and test 5 ("`run_id` is unchanged").
"""

from __future__ import annotations

import sqlite3

import pytest

from agentdx.store.migrations import trigger_names
from agentdx.store.sqlite import APPEND_ONLY_MESSAGE, RunRecord, Store, StoreError
from tests.unit.store.conftest import chain, populate
from tests.unit.store.factories import build_log, run_record_for


def test_discard_orphan_run_deletes_an_unsealed_runs_row_and_events(store: Store) -> None:
    """The one case this method exists for: an orphan is gone, cleanly, after discard."""
    events = build_log(spans=2)
    run_id = populate(store, events, seal=False)
    assert store.get_run(run_id) is not None
    assert list(store.read_events(run_id))

    store.discard_orphan_run(run_id)

    assert store.get_run(run_id) is None
    assert list(store.read_events(run_id)) == []


def test_discard_orphan_run_refuses_a_sealed_run(
    populated: tuple[Store, str, tuple[object, ...]],
) -> None:
    """A sealed run is never deleted by this method — `d78-plan.md` §7 test 4, directly.

    Refuses with `E-STORE-005` and changes nothing: the run row, its `sealed_at`, and every
    one of its events are byte-identical before and after the refused call.
    """
    store, run_id, _ = populated
    before_record = store.get_run(run_id)
    before_events = list(store.read_events(run_id))
    assert before_record is not None
    assert before_record.sealed

    with pytest.raises(StoreError) as excinfo:
        store.discard_orphan_run(run_id)
    assert excinfo.value.code == "E-STORE-005"

    after_record = store.get_run(run_id)
    assert after_record == before_record
    assert list(store.read_events(run_id)) == before_events


def test_discard_orphan_run_refuses_an_unknown_run(store: Store) -> None:
    """A run_id with no `runs` row at all is `E-STORE-004`, not a silent no-op."""
    with pytest.raises(StoreError) as excinfo:
        store.discard_orphan_run("r_does_not_exist")
    assert excinfo.value.code == "E-STORE-004"


def test_discard_orphan_run_leaves_the_append_only_triggers_fully_functional(
    store: Store,
) -> None:
    """Not just present by name afterward — still genuinely enforcing I2.

    Discards one orphaned run, then proves the triggers this method dropped and reinstated
    still abort a real UPDATE/DELETE against a *different*, unrelated run's events — the
    same live check `tests/unit/store/test_append_only.py` runs against a store that never
    called this method at all.
    """
    orphan_events = build_log(spans=1, run_id="r_orphan01")
    orphan_id = populate(store, orphan_events, seal=False)
    other_events = build_log(spans=2, run_id="r_other001")
    other_id = populate(store, other_events, seal=False)

    store.discard_orphan_run(orphan_id)

    present = {
        str(row[0])
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert set(trigger_names()) <= present

    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        store.connection.execute(
            "UPDATE events SET payload = ? WHERE run_id = ? AND seq = 0",
            ('{"tampered":true}', other_id),
        )
    assert APPEND_ONLY_MESSAGE in str(excinfo.value)

    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        store.connection.execute("DELETE FROM events WHERE run_id = ? AND seq = 0", (other_id,))
    assert APPEND_ONLY_MESSAGE in str(excinfo.value)


def test_a_fresh_run_can_reuse_the_run_id_after_discard(store: Store) -> None:
    """The point of "replace": the exact same `run_id` writes cleanly afterward.

    `d78-plan.md` §7 test 5 ("run_id is unchanged by all of the above") and test 3 ("an
    orphaned run proceeds to a real run") together, at the store layer: discarding an
    orphan is only useful if `create_run`/`append`/`seal` at that same id then succeed as
    if the orphan had never existed — not `E-STORE-010` again, and not events from the two
    attempts mixed together.
    """
    orphan_events = build_log(spans=1)
    run_id = populate(store, orphan_events, seal=False)
    store.discard_orphan_run(run_id)

    fresh_events = build_log(spans=3)
    record_fields = run_record_for(fresh_events)
    record_fields["run_id"] = run_id  # same id — this is the collision this method exists for
    store.create_run(RunRecord(**record_fields))  # type: ignore[arg-type]
    batch = chain(fresh_events)
    store.append(batch)
    store.seal(run_id, batch[-1].this_hash)

    record = store.get_run(run_id)
    assert record is not None
    assert record.sealed
    stored_events = list(store.read_events(run_id))
    assert len(stored_events) == len(fresh_events)
