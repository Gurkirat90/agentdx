"""Shared fixtures for the store suites: a temporary store and a populated run."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.events.canonical import build_chain
from agentdx.events.schema import Event
from agentdx.events.writer import ChainedEvent
from agentdx.store.snapshots import SnapshottingStore
from agentdx.store.sqlite import RunRecord, Store
from tests.unit.store.factories import build_log, run_record_for


@pytest.fixture
def config() -> StoreConfig:
    """Return a store configuration with a small snapshot interval.

    The interval is deliberately small so a test log of a few dozen events crosses it
    several times. The value comes from configuration in exactly the way production does —
    the point of the fixture is to exercise the configurability, not to bypass it.
    """
    return StoreConfig(snapshot_interval_events=5, duckdb_threshold_events=20_000)


@pytest.fixture
def store(tmp_path: Path, config: StoreConfig) -> Iterator[Store]:
    """Yield an open, empty store in a temporary directory."""
    opened = Store.open(tmp_path / "agentdx.db", config=config)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def snapshotting_store(tmp_path: Path, config: StoreConfig) -> Iterator[SnapshottingStore]:
    """Yield an open, empty store that materialises state snapshots on append."""
    opened = SnapshottingStore.open(tmp_path / "agentdx.db", config=config)
    try:
        yield opened
    finally:
        opened.close()


def chain(events: Sequence[Event]) -> list[ChainedEvent]:
    """Return the events paired with their hash chain, ready for `Store.append`."""
    return [
        ChainedEvent(event=event, prev_hash=prev, this_hash=this)
        for event, (prev, this) in zip(events, build_chain(events), strict=True)
    ]


def populate(store: Store, events: Sequence[Event], *, seal: bool = True) -> str:
    """Create the run row, append every event in batches, and optionally seal.

    Returns the `run_id`. Batching mirrors the writer's behaviour rather than inserting the
    whole log at once, so the tests exercise the same transaction boundaries production does.
    """
    record = RunRecord(**run_record_for(events))  # type: ignore[arg-type]
    store.create_run(record)
    batch = chain(events)
    size = store.config.append_batch_size
    for start in range(0, len(batch), size):
        store.append(batch[start : start + size])
    if seal:
        store.seal(record.run_id, batch[-1].this_hash)
    return record.run_id


@pytest.fixture
def populated(store: Store) -> tuple[Store, str, tuple[Event, ...]]:
    """Return an open store holding one sealed run, plus its id and its events."""
    events = build_log(spans=4)
    run_id = populate(store, events)
    return store, run_id, events
