# Storage contract — SQLite, DuckDB, snapshots and bundles

Companion to `docs/event-schema.md`. That document is the event contract; this one is where
those events live. Spec: PRD §27 (storage architecture), §20.4 (state reconstruction),
§20.7 (the `.agentdx` bundle), §31.3 and §31.9 (sharing and bundle trust), §32 NFR-10 and
NFR-13. Invariants in play: **I2** append-only, **I7** offline, **I8** privacy.

This file exists primarily so that every `E-STORE-NNN` and `E-BUNDLE-NNN` error message has
a live anchor to point at. The codes are part of the public contract — CI output and the
CLI branch on them — so renumbering one is a breaking change.

---

## 1. Files on disk

| File | Owner | Authoritative? |
|---|---|---|
| `agentdx.db` | `store/sqlite.py` | **Yes.** Runs, events, findings, scorecards, snapshots, scenarios |
| `agentdx.db-wal`, `-shm` | SQLite | Yes, until checkpointed. `Store.close()` checkpoints, so a single-file copy is a complete backup |
| `runs/<run_id>/events.parquet` | `store/duckdb.py` | No. A regenerable export |
| `cache.db` | `runtime/cache/` (P07) | Yes, separately. A different file so a shared cache carries no run history (PRD §27.2) |
| `*.agentdx` | `store/bundle.py` | No. A portable copy, self-verifying |

The data directory is a directory of files. Copying it is the backup (PRD §27.5).

## 2. Append-only, and what enforces it

`events` carries two `BEFORE` triggers, `events_no_update` and `events_no_delete`, which
`RAISE(ABORT, 'events are append-only')`. This is invariant **I2** as a database constraint
rather than an application convention: the refusal applies to the runner, to the API reader
process, to `sqlite3` on the command line, and to any future code that forgets.

The primary key `(run_id, seq)` is the other half — it stops history being rewritten by
re-insertion rather than in place.

Three tables are deliberately **not** append-only, because they hold derived data that is
regenerable from the log: `state_snapshots`, `findings`, `scorecards`. Deleting every row
of any of them costs recomputation and nothing else.

A migration cannot rewrite an event row unless it declares `rewrites_events=True`, in which
case the runner drops the triggers, applies it, and reinstates *and verifies* them inside
the same transaction. The declaration is a visible line in a diff; a trigger quietly dropped
inside a migration script would not be.

## 3. Crash behaviour (NFR-13)

WAL mode, one transaction per batch, `synchronous=NORMAL`. A process killed mid-run leaves
every committed batch durable and no partial batch at all. The surviving prefix is a valid
log: it decodes, it passes `validate_log`, its hash chain verifies, and its run row stays
`running` with a null `sealed_at` so nothing mistakes it for a complete run.

`synchronous=NORMAL` narrows the guarantee to process death rather than power loss. That is
the trade PRD §27.3 names deliberately; bundle export uses `FULL`.

## 4. Schema deviations from PRD §27.2

| Deviation | Why |
|---|---|
| `events` gains a `schema_version` column | The P02 event model marks `schema_version` `Volatility.STABLE`, so it is inside the canonical projection and inside every event's `this_hash`. A row without it cannot round-trip to the event that produced its hash. Reconstructing it from `runs.schema_version` would make each event's hash depend on a second table being present and correct — which it is not during a bundle import, where events land before run metadata is trusted |

Nothing was removed from §27.2's DDL.

## 5. Thresholds

Every one of these lives in `agentdx.toml` under `[store]` and is resolved through
`agentdx.config`. None is written inline in `store/`.

| Setting | Default | Spec |
|---|---|---|
| `duckdb_threshold_events` | 20000 | Q-43.2.2, PRD §27.1. At or above this, analysis routes through DuckDB over Parquet. A performance decision, never a semantic one — both paths produce identical results |
| `snapshot_interval_events` | 500 | PRD §20.4. Bounds state reconstruction at O(interval) |
| `append_batch_size` | 128 | PRD §27.3 |
| `synchronous` | `NORMAL` | PRD §27.3 |

Measured ingestion throughput is published in `bench/results/store-write-throughput.json`
and is cited wherever a rate is quoted `[bench:store-write-throughput.json]`.

## 6. Bundle format

A `.agentdx` file is a zip archive with exactly these members and no others:

```
manifest.json          schema/agentdx/bundle versions, run_id, hashes, created_at
run.json               run metadata, findings, scorecard, analysis version
events.jsonl           the full canonical event log, one canonical line per event
events.sha256          the canonical log hash, for cross-checking the manifest
events.chain           one `this_hash` per event, in seq order (bundle format 2)
scenario.yaml          the exact scenario text, including resolved defaults
cache/manifest.json    cache keys + response hashes this run requires
cache/entries.jsonl    the cache slice with bodies — only with --include-cache-bodies
calibration.json       the calibration profile used
graph.json             graph identity: nodes, edges, tools, hashes
```

Bundle format **2** added `events.chain`. Format 1 bundles cannot locate a tampered event
and are not produced by this build.

**Deviation from §20.7:** the PRD names `events.jsonl.zst` and `cache/entries.jsonl.zst`.
Zstd needs the `zstandard` distribution, which is outside the permitted dependency set
(AGENTS.md §2, ADR-004). The members are stored uncompressed *inside* a DEFLATE zip
container instead — the archive is still compressed, no dependency is added, and zstd inside
a zip would have been double compression. `manifest.json` records `compression` so a future
zstd bundle is distinguishable rather than merely different.

### Trust (§31.9)

An imported bundle is untrusted input. Import **never executes anything**:

- member names are checked against an allowlist, not a denylist of dangerous patterns;
- absolute names, `..` components and backslashes are refused;
- a member declaring more than 4 GiB is refused before it is read;
- the scenario is stored as text and never parsed as YAML;
- the graph is referenced by hash, never shipped as code;
- the archive is read **exactly once**; the bytes that were verified are the bytes that get
  stored. Verifying a path and then re-opening it to import is a TOCTOU: a file on a synced
  or shared directory can change between the two reads;
- the canonical log hash is **recomputed** from `events.jsonl` and compared with the
  manifest, which detects tampering; the per-event `events.chain` is then walked, which
  **locates** it — that is what makes PRD §36's "failed at event 1043" deliverable, and a
  rolling hash alone never could;
- import runs entirely inside one transaction, so an interruption rolls everything back and
  a retry succeeds. A half-written run row would otherwise fail the idempotence check on
  every subsequent attempt and burn the `run_id` permanently.

`--verify`'s re-execution against a matching *local* graph is a CLI concern; this module
cannot run a graph at all, which is the strongest available form of the guarantee.

Import is idempotent by `run_id` + `canonical_log_hash`. A *different* log under an existing
`run_id` is refused rather than silently replacing recorded history.

## 7. Error codes

Codes are stable and part of the public contract.

### `E-STORE-001`
The database has AgentDX tables but no `schema_meta` version row. It is not an AgentDX
store, or something bypassed the migrations. Recovery: use a different file.

### `E-STORE-002`
The database schema is newer than this build understands. Migrations are forward-only and
this build will not guess at a schema it does not know. Recovery: upgrade AgentDX.

### `E-STORE-003`
Migration versions are not exactly 1..N in ascending order. This is a build defect and is
caught at import time.

### `E-STORE-004`
Events were appended for a run with no `runs` row. Call `create_run` first, so the log has
recorded provenance.

### `E-STORE-005`
The run is sealed. The log is append-only and closed (I2).

### `E-STORE-006`
A migration left one or both append-only triggers missing, or a seal targeted an unknown
run. The migration is rolled back; the events table must be append-only in the database at
all times.

### `E-STORE-007`
An `append` batch spanned more than one run. A batch belongs to exactly one run.

### `E-STORE-008`
A major migration is pending and does not run automatically on open. Run `agentdx migrate`
after backing up the data directory.

### `E-STORE-009`
The run is already sealed with a different chain head. Resealing with the same head is a
no-op; resealing with a different one would replace recorded history.

### `E-STORE-010`
A run with this `run_id` already exists.

### `E-STORE-011`
The status is not one of PRD §27.2's values: `created`, `running`, `analysing`, `complete`,
`failed`, `aborted_guard`.

### `E-STORE-012`
The database refused WAL mode. WAL is what lets the API read a run while the runner writes
it (PRD §24.2); without it the live view would block the run.

### `E-STORE-013`
A stored column does not hold the shape the contract requires, or holds a value outside
`PayloadValue`. The row was not written by this store.

### `E-STORE-014`
A `state_write` payload lacks a string `key` or `value_hash`. The event contract requires
both (PRD §9.5), so this can only occur on a log that bypassed validation.

### `E-STORE-015`
A `state_snapshots` row is not the shape `snapshots.py` writes. Snapshots are derived data:
delete them for that run and reconstruction replays the log instead.

### `E-STORE-016`
The optional `duckdb` package is not installed. Analysis falls back to the SQLite path with
identical results and slower large aggregations. Install it with `uv sync`.

### `E-STORE-017`
The run has no events, so there is nothing to export.

### `E-STORE-018`
No Parquet export exists for this run. Run `export_parquet` first.

### `E-STORE-019`
The WAL checkpoint failed. The database is intact — the WAL remains a valid part of it —
but a `-wal` file remains beside the `.db`. Copy the whole data directory rather than the
`.db` alone, or the copy will be missing the most recent events. Reported as a warning
rather than swallowed, because it changes what "copy the file" means.

### `E-BUNDLE-001`
The bundle is corrupt, tampered with, or not a readable zip. When the event log was altered,
the message names the first affected event.

### `E-BUNDLE-002`
The bundle's event schema version or bundle format version is incompatible with this build.
Both versions are named. Recovery: upgrade AgentDX.

### `E-BUNDLE-003`
The run named for export does not exist in this store.

### `E-BUNDLE-004`
The run has no events to export.

### `E-BUNDLE-005`
The archive contains a member that is not permitted, appears twice, is a path traversal
attempt, or exceeds the per-member size cap. A bundle is data, not an archive to unpack
(§31.9). Duplicate names are refused because this reader resolves a repeated name to one
entry while another unzip tool resolves it to the other, so what was verified would not be
what a user inspecting the file sees.

### `E-BUNDLE-006`
The bundle is missing a required member and cannot be verified.

### `E-BUNDLE-007`
A run with this `run_id` already exists locally with a different canonical log hash. Import
is idempotent by `run_id` + hash and will not replace a recorded log.

### `E-CONFIG-001`
A configuration value could not be resolved to its declared type, is out of range, or names
a setting that does not exist. Reported rather than defaulted: a threshold the user believes
they set and which was silently ignored produces a measurement of a configuration nobody
chose.
