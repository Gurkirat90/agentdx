"""Migration 1 — the initial store schema, transcribed from PRD §27.2.

Every statement here is the PRD's DDL, in the PRD's order, with **one addition and no
removals**. The addition is documented below because an undeclared divergence in the
schema of an append-only log is the most expensive kind (CONTEXT.md §9).

**The addition: `events.schema_version`.** PRD §27.2's `events` table omits it, but the P02
event model has `schema_version` as a required field marked `Volatility.STABLE` — it is
*inside* the canonical projection and therefore inside every event's `this_hash`. A row
written without it cannot round-trip to the event that produced its hash, so a stored log
could not be re-verified from its own table. The alternatives were both worse:
reconstructing the value from `runs.schema_version` makes the canonical hash of every event
depend on a second table being present and correct — which it is not during a bundle
import, where events land before their run row — and dropping the field from the projection
is a schema change P03 has no authority to make. The column costs two bytes per row.
Recorded as a deviation; owner-approved before any store code was written.

**PRAGMAs are not here.** `journal_mode`, `synchronous` and `foreign_keys` from §27.2's
first line are connection- and file-level settings, not schema. `sqlite.py` applies them on
every open, so a connection opened by a second process (the API reader, PRD §24.2) gets
them too — a migration could not guarantee that.
"""

from __future__ import annotations

from typing import Final

from agentdx.store.migrations._base import Migration

_RUNS: Final = """
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, scenario_id TEXT, scenario_hash TEXT NOT NULL,
  graph_hash TEXT NOT NULL, mode TEXT NOT NULL, seed INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL, sealed_at TEXT,
  virtual_makespan_ms INTEGER, wall_makespan_ms INTEGER,
  canonical_log_hash TEXT, event_count INTEGER,
  baseline_of TEXT REFERENCES runs(run_id),
  replay_of  TEXT REFERENCES runs(run_id),
  explore_parent TEXT REFERENCES runs(run_id),
  agentdx_version TEXT NOT NULL, schema_version INTEGER NOT NULL,
  delay_schedule TEXT, calibration_id TEXT, determinism_quality TEXT
)
"""

_EVENTS: Final = """
CREATE TABLE events (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  schema_version INTEGER NOT NULL,
  sched_step INTEGER NOT NULL,
  virtual_ts_ms INTEGER NOT NULL, wall_ts_ms INTEGER NOT NULL,
  agent_id TEXT, clock_slot TEXT, type TEXT NOT NULL, span_id TEXT,
  vclock TEXT NOT NULL,
  causal_parents TEXT NOT NULL,
  fault_id TEXT, payload TEXT NOT NULL,
  prev_hash TEXT, this_hash TEXT,
  PRIMARY KEY (run_id, seq)
) WITHOUT ROWID
"""

_INDEXES: Final = (
    "CREATE INDEX idx_events_type   ON events(run_id, type, seq)",
    "CREATE INDEX idx_events_agent  ON events(run_id, agent_id, seq)",
    "CREATE INDEX idx_events_vts    ON events(run_id, virtual_ts_ms)",
    "CREATE INDEX idx_events_span   ON events(run_id, span_id)",
    "CREATE INDEX idx_events_fault  ON events(run_id, fault_id) WHERE fault_id IS NOT NULL",
)

_TRIGGERS: Final = (
    """
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END
""",
    """
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END
""",
)

_FINDINGS: Final = """
CREATE TABLE findings (
  finding_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  type TEXT NOT NULL, subtype TEXT, severity TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL,
  evidence TEXT NOT NULL,
  recommendation TEXT, suppressed_by TEXT, repro_scenario_path TEXT,
  analysis_version TEXT NOT NULL
)
"""

_SCORECARDS: Final = """
CREATE TABLE scorecards (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  payload TEXT NOT NULL, analysis_version TEXT NOT NULL, computed_at TEXT NOT NULL
)
"""

_STATE_SNAPSHOTS: Final = """
CREATE TABLE state_snapshots (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
)
"""

_SCENARIOS: Final = """
CREATE TABLE scenarios (
  scenario_id TEXT PRIMARY KEY, path TEXT, content TEXT NOT NULL,
  content_hash TEXT NOT NULL, version INTEGER NOT NULL
)
"""

_SCHEMA_META: Final = "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


MIGRATION: Final = Migration(
    version=1,
    name="initial schema (PRD §27.2)",
    statements=(
        _SCHEMA_META,
        _RUNS,
        _EVENTS,
        *_INDEXES,
        *_TRIGGERS,
        _FINDINGS,
        _SCORECARDS,
        _STATE_SNAPSHOTS,
        _SCENARIOS,
    ),
    triggers=_TRIGGERS,
)

__all__ = ["MIGRATION"]
