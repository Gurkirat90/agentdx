"""The DuckDB analytical path: Parquet export and the §27.4 views.

PRD §27.1 divides the work: SQLite owns ingestion, point reads and authoritative data;
DuckDB reads a Parquet export of a sealed run and answers columnar aggregations. Three
constraints from that section shape everything here.

**DuckDB never owns authoritative data.** It is read-only over an export. Deleting every
`.parquet` file loses nothing — `export_parquet` regenerates them from SQLite.

**The switchover is a performance decision, never a semantic one.** PRD §27.1 is explicit,
and the threshold is Q-43.2.2 with a default of 20 000 events. It comes from
`StoreConfig.duckdb_threshold_events` and appears nowhere else as a literal, because week 5
is expected to tune it from benchmarks. Both paths must produce identical results;
`tests/integration/store/test_duckdb_parity.py` asserts that for the `spans` view rather
than trusting the claim.

**An optional accelerator must not be able to hard-fail the product.** If `duckdb` is not
importable, `analytics_available()` is False and every caller falls back to the SQLite path
with a warning (PRD §27.4, and §36's cross-cutting rule 1: never fail silently). The
warning is returned as data rather than logged, so the caller decides how to surface it.

**Export is deterministic in content, not in bytes.** The rows written are ordered by
`seq`, so two exports of the same run carry identical data in identical order. Parquet's
container bytes may still differ between DuckDB versions, which is why nothing in this
system hashes a Parquet file — the canonical log hash is computed over events, never over
an export (I1, PRD §10.7).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentdx.config import StoreConfig
from agentdx.events.canonical import encode_value
from agentdx.events.schema import Event
from agentdx.store.sqlite import Store, StoreError

PARQUET_FILENAME: Final = "events.parquet"
"""The export filename PRD §27.4 reads: `runs/<run_id>/events.parquet`."""

_EV_COLUMNS: Final = (
    "run_id",
    "seq",
    "schema_version",
    "sched_step",
    "virtual_ts_ms",
    "wall_ts_ms",
    "agent_id",
    "clock_slot",
    "type",
    "span_id",
    "vclock",
    "causal_parents",
    "fault_id",
    "payload",
)

_EV_DDL: Final = """
CREATE TABLE ev (
  run_id VARCHAR, seq BIGINT, schema_version INTEGER, sched_step BIGINT,
  virtual_ts_ms BIGINT, wall_ts_ms BIGINT, agent_id VARCHAR, clock_slot VARCHAR,
  type VARCHAR, span_id VARCHAR, vclock VARCHAR, causal_parents VARCHAR,
  fault_id VARCHAR, payload VARCHAR
)
"""

SPANS_VIEW_SQL: Final = """
CREATE VIEW spans AS
SELECT s.span_id, s.agent_id,
       s.virtual_ts_ms                          AS start_ms,
       e.virtual_ts_ms                          AS end_ms,
       json_extract_string(s.payload,'$.kind')  AS kind,
       json_extract_string(e.payload,'$.status') AS status
FROM ev s JOIN ev e
  ON s.span_id = e.span_id AND s.type='span_start' AND e.type='span_end'
"""
"""The span materialisation of PRD §27.4, verbatim.

Kept as a named constant so the SQLite fallback in `spans_via_sqlite` can be asserted equal
to it rather than being a second, hand-written definition of what a span is.
"""


@dataclass(frozen=True, slots=True)
class Span:
    """One row of the §27.4 `spans` view.

    Guarantees: produced identically by the DuckDB path and the SQLite fallback. `end_ms`
    and `status` come from the matching `span_end`, so a span whose `span_end` is missing —
    the normal shape at the tail of a crashed run — does not appear at all. That is an
    inner join in the PRD's own SQL and it is the honest answer: an unterminated span has
    no duration.
    """

    span_id: str
    agent_id: str | None
    start_ms: int
    end_ms: int
    kind: str | None
    status: str | None


@dataclass(frozen=True, slots=True)
class AnalysisRoute:
    """Which path analysis should take for a run, and why.

    Returned rather than decided internally so the choice is visible in the CLI and the API
    (PRD §36 rule 1: nothing that degrades analysis quality is silent). `warning` is
    non-None exactly when the route is not the one the threshold asked for.
    """

    use_duckdb: bool
    event_count: int
    threshold: int
    warning: str | None = None


def analytics_available() -> bool:
    """Return True iff the optional `duckdb` package can be imported.

    Guarantees: does not import DuckDB as a side effect of asking — `find_spec` only
    resolves it — so a process that never analyses never pays the import cost.
    """
    return importlib.util.find_spec("duckdb") is not None


def choose_route(store: Store, run_id: str, config: StoreConfig | None = None) -> AnalysisRoute:
    """Decide whether a run is analysed through DuckDB or directly from SQLite.

    The threshold is Q-43.2.2 and is read from configuration on every call; there is no
    cached copy and no literal anywhere in this module.

    Guarantees: when DuckDB is unavailable the route is SQLite *with a warning*, never a
    failure — PRD §27.4 requires the product not to hard-fail on an optional accelerator.

    Args:
        store: The store holding the run.
        run_id: The run to route.
        config: Overrides the store's configuration; used by tests and by `--explain`.
    """
    settings = config if config is not None else store.config
    count = store.event_count(run_id)
    threshold = settings.duckdb_threshold_events
    wants_duckdb = count >= threshold
    if wants_duckdb and not analytics_available():
        return AnalysisRoute(
            use_duckdb=False,
            event_count=count,
            threshold=threshold,
            warning=(
                f"[E-STORE-016] run {run_id} has {count} events (threshold {threshold}) and "
                f"would be analysed through DuckDB, but the optional `duckdb` package is not "
                f"installed. Falling back to the SQLite path: results are identical, and "
                f"large aggregations will be slower. Install it with `uv sync`"
            ),
        )
    return AnalysisRoute(use_duckdb=wants_duckdb, event_count=count, threshold=threshold)


def export_parquet(store: Store, run_id: str, dest: Path | None = None) -> Path:
    """Export a run's events to Parquet and return the file written (PRD §27.4).

    Rows are written in `seq` order and carry the same columns the SQLite `events` table
    does, so a query written against one runs unchanged against the other.

    Guarantees: writes to a temporary file and renames, so a reader never observes a
    half-written export. Regenerable: the Parquet file is derived data and deleting it
    loses nothing.

    Args:
        store: The store holding the run.
        run_id: The run to export.
        dest: Destination file. Defaults to `<db parent>/runs/<run_id>/events.parquet`.

    Returns:
        The path written.

    Raises:
        StoreError: `E-STORE-016` DuckDB is not installed · `E-STORE-017` the run has no
            events, so there is nothing to export.
    """
    if not analytics_available():
        raise StoreError(
            "E-STORE-016",
            "the optional `duckdb` package is not installed, so Parquet export is "
            "unavailable. Analysis still works through the SQLite path",
        )
    count = store.event_count(run_id)
    if count == 0:
        raise StoreError("E-STORE-017", f"run {run_id!r} has no events to export")

    target = dest if dest is not None else default_parquet_path(store, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(target.suffix + ".partial")

    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(_EV_DDL)
        placeholders = ", ".join("?" * len(_EV_COLUMNS))
        connection.executemany(
            f"INSERT INTO ev VALUES ({placeholders})",  # noqa: S608 — fixed column tuple
            [_parquet_row(event) for event in store.read_events(run_id)],
        )
        connection.execute(
            f"COPY (SELECT * FROM ev ORDER BY seq) "  # noqa: S608 — path is an escaped literal
            f"TO {_sql_literal(str(staging))} (FORMAT PARQUET)"
        )
    finally:
        connection.close()
    staging.replace(target)
    return target


def default_parquet_path(store: Store, run_id: str) -> Path:
    """Return the conventional export path for a run: `runs/<run_id>/events.parquet`.

    Guarantees: derived from the store's own file location, so a data directory remains a
    self-contained directory of files that can be copied as a backup (PRD §27.5).
    """
    return store.path.parent / "runs" / run_id / PARQUET_FILENAME


@contextmanager
def attached(parquet: Path) -> Iterator[object]:
    """Yield a DuckDB connection with the §27.4 `ev` and `spans` views defined.

    Guarantees: read-only over the Parquet file; the connection is in-memory and is closed
    on exit whether or not the body raised. DuckDB never owns authoritative data, so
    nothing here writes.

    Raises:
        StoreError: `E-STORE-016` DuckDB is not installed · `E-STORE-018` the export does
            not exist, which means `export_parquet` was not run for this run.
    """
    if not analytics_available():
        raise StoreError("E-STORE-016", "the optional `duckdb` package is not installed")
    if not parquet.is_file():
        raise StoreError(
            "E-STORE-018",
            f"no Parquet export at {parquet}; run export_parquet for this run first",
        )
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        # DuckDB refuses bound parameters in DDL ("this type of statement can't be
        # prepared"), so the path is interpolated as an escaped SQL literal instead. The
        # value is a local filesystem path this module constructed, and `_sql_literal`
        # doubles every quote, so the interpolation cannot terminate the string early.
        connection.execute(
            f"CREATE VIEW ev AS SELECT * FROM read_parquet({_sql_literal(str(parquet))})"  # noqa: S608
        )
        connection.execute(SPANS_VIEW_SQL)
        yield connection
    finally:
        connection.close()


def _sql_literal(text: str) -> str:
    """Return `text` as a single-quoted SQL string literal, with quotes doubled.

    Guarantees: the result cannot terminate the literal early, so interpolating it into a
    statement is safe for the one thing this module interpolates — a filesystem path it
    constructed itself. Used only where DuckDB refuses a bound parameter (DDL).
    """
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def spans_via_duckdb(parquet: Path) -> tuple[Span, ...]:
    """Return the `spans` view over an exported run, ordered for a stable comparison.

    Guarantees: identical to `spans_via_sqlite` for the same run — that equality is the
    executable form of PRD §27.1's "the behaviour is identical either way".
    """
    with attached(parquet) as connection:
        rows = connection.execute(  # type: ignore[attr-defined]
            "SELECT span_id, agent_id, start_ms, end_ms, kind, status FROM spans "
            "ORDER BY start_ms, span_id"
        ).fetchall()
    return tuple(_span_from_row(row) for row in rows)


def spans_via_sqlite(store: Store, run_id: str) -> tuple[Span, ...]:
    """Return the `spans` view computed directly from SQLite, for runs below the threshold.

    The SQL mirrors `SPANS_VIEW_SQL` join for join. SQLite's `json_extract` returns the
    unquoted scalar for a string, which is what DuckDB's `json_extract_string` returns, so
    the two produce the same values rather than one producing `"ok"` and the other `ok`.

    Guarantees: identical output to `spans_via_duckdb`, asserted by the parity test.
    """
    rows = store.connection.execute(
        "SELECT s.span_id, s.agent_id, s.virtual_ts_ms AS start_ms, "
        "       e.virtual_ts_ms AS end_ms, "
        "       json_extract(s.payload, '$.kind') AS kind, "
        "       json_extract(e.payload, '$.status') AS status "
        "FROM events s JOIN events e "
        "  ON s.run_id = e.run_id AND s.span_id = e.span_id "
        " AND s.type = 'span_start' AND e.type = 'span_end' "
        "WHERE s.run_id = ? "
        "ORDER BY start_ms, s.span_id",
        (run_id,),
    ).fetchall()
    return tuple(_span_from_row(row) for row in rows)


def spans(store: Store, run_id: str, config: StoreConfig | None = None) -> tuple[Span, ...]:
    """Return the `spans` view by whichever route the threshold selects.

    Guarantees: the returned value does not depend on the route. If the DuckDB route is
    selected but no export exists, this exports first — the switchover is meant to be
    invisible to the caller.
    """
    route = choose_route(store, run_id, config)
    if not route.use_duckdb:
        return spans_via_sqlite(store, run_id)
    parquet = default_parquet_path(store, run_id)
    if not parquet.is_file():
        parquet = export_parquet(store, run_id)
    return spans_via_duckdb(parquet)


def _parquet_row(event: Event) -> tuple[str | int | None, ...]:
    """Return the export row for an event, matching `_EV_COLUMNS`.

    The three composite columns are encoded with `canonical.encode_value`, exactly as the
    SQLite columns are, so `json_extract_string` over Parquet and `json_extract` over
    SQLite see the same text.
    """
    return (
        event.run_id,
        event.seq,
        event.schema_version,
        event.sched_step,
        event.virtual_ts_ms,
        event.wall_ts_ms,
        event.agent_id,
        event.clock_slot,
        str(event.type),
        event.span_id,
        encode_value(dict(event.vclock)),
        encode_value(list(event.causal_parents)),
        event.fault_id,
        encode_value(dict(event.payload)),
    )


def _span_from_row(row: Sequence[object]) -> Span:
    """Return the `Span` a view row represents, normalising the two engines' types."""
    return Span(
        span_id=str(row[0]),
        agent_id=None if row[1] is None else str(row[1]),
        start_ms=int(str(row[2])),
        end_ms=int(str(row[3])),
        kind=None if row[4] is None else str(row[4]).strip('"'),
        status=None if row[5] is None else str(row[5]).strip('"'),
    )


__all__ = [
    "PARQUET_FILENAME",
    "SPANS_VIEW_SQL",
    "AnalysisRoute",
    "Span",
    "analytics_available",
    "attached",
    "choose_route",
    "default_parquet_path",
    "export_parquet",
    "spans",
    "spans_via_duckdb",
    "spans_via_sqlite",
]
