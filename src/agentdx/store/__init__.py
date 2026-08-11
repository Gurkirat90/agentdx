"""Persistence: SQLite (WAL), DuckDB views, migrations, snapshots and bundles. No analysis logic.

Append-only immutability is enforced by SQLite triggers, not by convention (I2).
May import `events`; must not import `runtime`, `sdk` or `analysis` (CONTEXT.md §4).

The four modules and what each is responsible for:

* `sqlite.py`    — the schema, WAL configuration, the `Store` API, and the `EventSink`
  implementation an `EventWriter` writes into. The authoritative data lives here.
* `snapshots.py` — state reconstruction for §20.4 time travel. Snapshots are an
  optimisation over the log and never an alternative to it.
* `duckdb.py`    — Parquet export and the §27.4 analytical views. Read-only, optional, and
  never authoritative; if DuckDB is missing, analysis falls back to SQLite with a warning.
* `bundle.py`    — the `.agentdx` export/import/verify format. An imported bundle is data,
  never code.

`duckdb.py` is deliberately importable without the `duckdb` package installed — it is an
optional accelerator (PRD §27.4) — so it is not re-exported here. Import it directly.
"""

from agentdx.store.migrations import MigrationError, latest_version, migrate
from agentdx.store.snapshots import (
    SnapshottingStore,
    StateFold,
    ValueRef,
    rebuild_snapshots,
    state_at,
    state_by_replay,
)
from agentdx.store.sqlite import (
    APPEND_ONLY_MESSAGE,
    RUN_STATUSES,
    FindingRecord,
    RunRecord,
    ScenarioRecord,
    ScorecardRecord,
    Store,
    StoreError,
)

__all__ = [
    "APPEND_ONLY_MESSAGE",
    "RUN_STATUSES",
    "FindingRecord",
    "MigrationError",
    "RunRecord",
    "ScenarioRecord",
    "ScorecardRecord",
    "SnapshottingStore",
    "StateFold",
    "Store",
    "StoreError",
    "ValueRef",
    "latest_version",
    "migrate",
    "rebuild_snapshots",
    "state_at",
    "state_by_replay",
]
