"""Snapshots accelerate §20.4 reconstruction and must never change its answer.

Design constraint 6: a snapshot is an optimisation, never the source of truth, and must
always be reproducible by replaying events. Both halves are asserted directly — the first
by comparing `state_at` (snapshot-accelerated) with `state_by_replay` (pure fold) at random
timestamps, the second by rebuilding the snapshot rows from the sealed log and comparing
them byte for byte with the rows written during ingestion.

The randomness is seeded. A test whose failures cannot be reproduced is not evidence.
"""

from __future__ import annotations

import random

import pytest

from agentdx.events.schema import Event
from agentdx.store.snapshots import (
    SnapshottingStore,
    StateFold,
    is_snapshot_seq,
    rebuild_snapshots,
    state_at,
    state_by_replay,
    stored_snapshots,
)
from agentdx.store.sqlite import Store
from tests.unit.store.conftest import populate
from tests.unit.store.factories import build_log

SEED = 42
TIMESTAMP_SAMPLES = 20
"""The definition-of-done asks for 20 random virtual timestamps. `test_..._at_every_event`
below additionally covers every timestamp in the log exhaustively, which subsumes it — the
sampled test is kept because it is the one that scales to a 200 000-event log (NFR-11)."""


@pytest.fixture
def with_snapshots(snapshotting_store: SnapshottingStore) -> tuple[Store, str, tuple[Event, ...]]:
    """Return a store holding one sealed run whose snapshots were written on ingestion."""
    events = build_log(spans=12)
    run_id = populate(snapshotting_store, events)
    return snapshotting_store, run_id, events


def test_snapshots_were_actually_written(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """The fixture crosses the snapshot interval several times.

    Without this the equivalence tests below could pass vacuously against a run that has no
    snapshots at all — in which case `state_at` and `state_by_replay` are the same code path
    and prove nothing.
    """
    store, run_id, events = with_snapshots
    rows = stored_snapshots(store, run_id)
    interval = store.config.snapshot_interval_events
    assert len(rows) >= 3
    assert sorted(rows) == [e.seq for e in events if is_snapshot_seq(e.seq, interval)]


def test_state_at_equals_replay_at_20_random_timestamps(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """Reconstruction from snapshots equals a pure replay at 20 random virtual timestamps.

    This is the definition-of-done check for design constraint 6.
    """
    store, run_id, events = with_snapshots
    rng = random.Random(SEED)  # noqa: S311 — sampling test inputs, not cryptography
    highest = events[-1].virtual_ts_ms
    for _ in range(TIMESTAMP_SAMPLES):
        virtual_ts = rng.randint(0, highest + 50)
        assert state_at(store, run_id, virtual_ts) == state_by_replay(store, run_id, virtual_ts)


def test_state_at_equals_replay_at_every_event(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """The equivalence holds at every timestamp in the log, not only at sampled ones.

    Exhaustive on a small log, which is what catches an off-by-one at a snapshot boundary —
    precisely the bug random sampling is least likely to find.
    """
    store, run_id, events = with_snapshots
    for event in events:
        virtual_ts = event.virtual_ts_ms
        assert state_at(store, run_id, virtual_ts) == state_by_replay(store, run_id, virtual_ts)


def test_deleting_every_snapshot_changes_nothing(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """The log is the source of truth: dropping all snapshots costs latency and nothing else.

    If this ever fails, snapshots have become an alternative history rather than a cache of
    one, which is the failure design constraint 6 exists to prevent.
    """
    store, run_id, events = with_snapshots
    rng = random.Random(SEED)  # noqa: S311 — sampling test inputs, not cryptography
    highest = events[-1].virtual_ts_ms
    timestamps = [rng.randint(0, highest + 50) for _ in range(TIMESTAMP_SAMPLES)]
    before = [state_at(store, run_id, t) for t in timestamps]

    store.connection.execute("DELETE FROM state_snapshots WHERE run_id = ?", (run_id,))
    assert stored_snapshots(store, run_id) == {}

    assert [state_at(store, run_id, t) for t in timestamps] == before


def test_stored_snapshots_are_reproducible_by_replaying_the_log(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """Rows written during ingestion are byte-identical to rows rebuilt from the log.

    Byte-identical, not merely equivalent: both paths encode through
    `canonical.encode_value`, so a difference in key order would be a difference in the
    stored bytes and would show up here rather than surviving as a latent inconsistency.
    """
    store, run_id, events = with_snapshots
    rebuilt = rebuild_snapshots(events, store.config.snapshot_interval_events)
    assert stored_snapshots(store, run_id) == rebuilt


def test_reconstruction_is_bounded_by_the_snapshot_interval(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """`state_at` reads at most `snapshot_interval_events` events after the snapshot.

    PRD §20.4's whole justification is that reconstruction is O(interval) regardless of run
    length. Asserted by counting the events actually read, because a correct-but-linear
    implementation would pass every other test in this file.
    """
    store, run_id, events = with_snapshots
    interval = store.config.snapshot_interval_events
    target = events[-1]
    read_count = 0
    original = store.read_events

    def counting_read(*args: object, **kwargs: object) -> object:
        nonlocal read_count
        for event in original(*args, **kwargs):  # type: ignore[arg-type]
            read_count += 1
            yield event

    store.read_events = counting_read  # type: ignore[assignment, method-assign]
    try:
        state_at(store, run_id, target.virtual_ts_ms)
    finally:
        store.read_events = original  # type: ignore[method-assign]
    assert read_count <= interval


def test_state_before_the_first_event_is_empty(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """A timestamp preceding the run reconstructs to an empty state, not an error."""
    store, run_id, _ = with_snapshots
    assert state_at(store, run_id, -1) == {}
    assert state_by_replay(store, run_id, -1) == {}


def test_a_value_ref_records_hash_writer_and_seq(
    with_snapshots: tuple[Store, str, tuple[Event, ...]],
) -> None:
    """Reconstruction carries the identity of a value and who wrote it (PRD §20.4, I8).

    With `capture_bodies=False` there is no body, and that is the honest result rather than
    a gap — the identity of the value and its writer is what conflict analysis needs.
    """
    store, run_id, events = with_snapshots
    state = state_at(store, run_id, events[-1].virtual_ts_ms)
    assert state
    for key, ref in sorted(state.items()):
        assert ref.value_hash.startswith("blake2b:")
        assert ref.writer in {"planner", "coder", "reviewer"}
        assert ref.seq >= 0
        assert ref.body is None, f"{key} carries a body although the run captured none"


def test_last_write_wins_within_the_fold() -> None:
    """The fold keeps the most recent write for a key, which is what the log records."""
    events = build_log(spans=8)
    fold = StateFold()
    for event in events:
        fold.apply(event)
    state = fold.state()
    for key, ref in state.items():
        latest = max(
            (e for e in events if e.payload.get("key") == key and e.type.value == "state_write"),
            key=lambda e: e.seq,
        )
        assert ref.seq == latest.seq
