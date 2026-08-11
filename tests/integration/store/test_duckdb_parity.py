"""PRD §27.1: the SQLite→DuckDB switchover is a performance decision, never a semantic one.

The PRD states that "a test asserts both paths produce identical analysis output". This is
that test. It matters because the switchover is invisible to the caller: a run that grows
past the threshold silently changes engine, and if the two engines disagreed, a finding
would appear or vanish with run length rather than with behaviour — the most confusing
possible failure.

Analysis proper lands at P10–P12, so what is compared here is the only analytical surface
this prompt owns: the §27.4 `spans` view, plus the raw event projection the views are built
on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.store import duckdb as analytics
from agentdx.store.sqlite import Store
from tests.unit.store.conftest import populate
from tests.unit.store.factories import build_log

pytestmark = pytest.mark.skipif(
    not analytics.analytics_available(),
    reason="duckdb is an optional accelerator (PRD §27.4); the SQLite path is the fallback",
)


@pytest.fixture
def populated_store(tmp_path: Path) -> tuple[Store, str]:
    """Return a store holding one sealed run with real span structure."""
    store = Store.open(tmp_path / "agentdx.db", config=StoreConfig())
    run_id = populate(store, build_log(spans=12))
    try:
        yield store, run_id  # type: ignore[misc]
    finally:
        store.close()


def test_both_paths_produce_identical_spans(populated_store: tuple[Store, str]) -> None:
    """The `spans` view is identical whether computed by DuckDB or by SQLite.

    Identical as a tuple of `Span` objects, not merely "the same number of rows" — the kind
    and status strings come from two different JSON extraction functions, which is exactly
    where a silent divergence would live.
    """
    store, run_id = populated_store
    parquet = analytics.export_parquet(store, run_id)

    via_duckdb = analytics.spans_via_duckdb(parquet)
    via_sqlite = analytics.spans_via_sqlite(store, run_id)

    assert via_duckdb == via_sqlite
    assert via_sqlite, "the fixture produced no spans; the comparison would be vacuous"


def test_the_route_does_not_change_the_answer(populated_store: tuple[Store, str]) -> None:
    """`spans()` returns the same value whichever side of the threshold the run falls.

    The threshold is forced both ways on the *same* run, so the only thing that differs is
    the engine.
    """
    store, run_id = populated_store
    count = store.event_count(run_id)

    below = analytics.spans(store, run_id, StoreConfig(duckdb_threshold_events=count + 1))
    above = analytics.spans(store, run_id, StoreConfig(duckdb_threshold_events=1))

    assert below == above


def test_export_is_regenerable_and_atomic(populated_store: tuple[Store, str]) -> None:
    """Deleting the Parquet export loses nothing; regenerating gives the same rows.

    DuckDB never owns authoritative data (PRD §27.4). The `.partial` staging file must also
    be gone, so a reader can never observe a half-written export.
    """
    store, run_id = populated_store
    first = analytics.export_parquet(store, run_id)
    rows = analytics.spans_via_duckdb(first)

    first.unlink()
    second = analytics.export_parquet(store, run_id)

    assert second == first
    assert analytics.spans_via_duckdb(second) == rows
    assert not list(second.parent.glob("*.partial"))


def test_the_export_carries_every_event(populated_store: tuple[Store, str]) -> None:
    """The Parquet export holds the same events, in seq order, as the SQLite table."""
    store, run_id = populated_store
    parquet = analytics.export_parquet(store, run_id)

    with analytics.attached(parquet) as connection:
        rows = connection.execute(  # type: ignore[attr-defined]
            "SELECT seq, type, schema_version FROM ev ORDER BY seq"
        ).fetchall()

    events = tuple(store.read_events(run_id))
    assert [int(r[0]) for r in rows] == [e.seq for e in events]
    assert [str(r[1]) for r in rows] == [str(e.type) for e in events]
    assert [int(r[2]) for r in rows] == [e.schema_version for e in events]


def test_exporting_an_empty_run_is_refused(tmp_path: Path) -> None:
    """`E-STORE-017`: there is nothing to export for a run with no events."""
    from agentdx.store.sqlite import RunRecord, StoreError

    with Store.open(tmp_path / "agentdx.db") as store:
        store.create_run(
            RunRecord(
                run_id="r_empty",
                scenario_hash="h",
                graph_hash="h",
                mode="replay",
                seed=42,
                status="created",
                created_at="2026-08-11T09:00:00Z",
                agentdx_version="0.1.0",
            )
        )
        with pytest.raises(StoreError) as excinfo:
            analytics.export_parquet(store, "r_empty")
        assert excinfo.value.code == "E-STORE-017"


def test_querying_a_missing_export_is_refused(tmp_path: Path) -> None:
    """`E-STORE-018`: the error names the fix, rather than only the problem."""
    from agentdx.store.sqlite import StoreError

    with pytest.raises(StoreError) as excinfo:
        with analytics.attached(tmp_path / "nope.parquet"):
            pass
    assert excinfo.value.code == "E-STORE-018"
    assert "export_parquet" in str(excinfo.value)
