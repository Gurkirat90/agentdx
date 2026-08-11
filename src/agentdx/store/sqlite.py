"""The SQLite event store: schema, WAL, append-only enforcement and the `Store` API.

This module implements the `EventSink` protocol declared in `events/writer.py` (PRD §27.2,
§27.3). Three properties are load-bearing and each is enforced rather than intended.

**Append-only is enforced by the database (I2).** `events_no_update` and `events_no_delete`
are BEFORE triggers that `RAISE(ABORT)`. Nothing in this module issues an UPDATE or DELETE
against `events`, but that is not the guarantee — the guarantee is that nothing *can*, from
this process, from the API reader, from `sqlite3` on the command line, or from a later
prompt that forgets. Application-level discipline is a convention; a trigger is a
constraint. `tests/unit/store/test_append_only.py` attempts both statements and asserts the
abort.

**A crashed run leaves a readable prefix (NFR-13).** WAL mode plus one transaction per
batch means every committed batch is durable against process death, and an uncommitted one
leaves no partial rows. `synchronous=NORMAL` (PRD §27.3) narrows the guarantee to process
death rather than power loss, which is the deliberate trade the PRD names. The prefix is a
valid log: `validate_log` accepts a log with no `run_end`, the hash chain verifies over it,
and the run row stays `running` with a null `sealed_at` so nothing mistakes it for
complete. `tests/integration/store/test_crash_partial_log.py` SIGKILLs a writer mid-run and
asserts all of that.

**Stored rows round-trip to the events that produced them.** `read_events` reconstructs an
`Event` whose `canonical_bytes` are identical to those of the event handed to `append`. It
has to be: `this_hash` is computed over the canonical projection, so a store that could not
reproduce those bytes could not verify its own chain. The composite columns are written
with `canonical.encode_value` — never `json.dumps` — because a second serialiser silently
breaks byte-stability, and read back with the stdlib parser under the same float refusal
the event contract applies (ruling R4).

What this module deliberately does **not** do: validate (that is `events/validators.py`),
canonicalise (that is `events/canonical.py`), or analyse anything.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final

from agentdx.config import StoreConfig
from agentdx.events.canonical import (
    CHAIN_GENESIS,
    FloatNotPermittedError,
    canonical_log_hash,
    chain_hash,
    encode_value,
)
from agentdx.events.schema import SCHEMA_VERSION, Event, EventType, PayloadValue
from agentdx.events.writer import ChainedEvent
from agentdx.store.migrations import MigrationError, current_version, latest_version, migrate

_DOCS: Final = "docs/storage.md"

APPEND_ONLY_MESSAGE: Final = "events are append-only"
"""The exact text the `events_no_update` / `events_no_delete` triggers abort with.

Exported so a test asserts against the message the database actually produces rather than
a copy of it, and so `agentdx doctor` can recognise the abort.
"""

_EVENT_COLUMNS: Final = (
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
    "prev_hash",
    "this_hash",
)

_INSERT_EVENT: Final = (
    f"INSERT INTO events ({', '.join(_EVENT_COLUMNS)}) "  # noqa: S608 — fixed column tuple
    f"VALUES ({', '.join('?' * len(_EVENT_COLUMNS))})"
)

_SELECT_EVENT: Final = f"SELECT {', '.join(_EVENT_COLUMNS)} FROM events"  # noqa: S608

RUN_STATUSES: Final = ("created", "running", "analysing", "complete", "failed", "aborted_guard")
"""The `runs.status` values of PRD §27.2, in lifecycle order."""


class StoreError(RuntimeError):
    """A storage operation was refused or failed.

    Guarantees: carries a stable `E-STORE-NNN` code plus a docs anchor (AGENTS.md §4), and
    never leaves a partially written batch — every write path is one transaction.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from its stable code and a human-readable explanation."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One row of the `runs` table (PRD §27.2).

    Guarantees: mirrors the DDL field for field, so adding a column is a change in two
    places (the migration and this dataclass) that the type checker forces to agree.
    `created_at` and `sealed_at` are ISO-8601 UTC strings supplied by the caller — the
    store never reads a clock to fill them, because a value the store invented would not be
    the value `run_start` recorded.
    """

    run_id: str
    scenario_hash: str
    graph_hash: str
    mode: str
    seed: int
    status: str
    created_at: str
    agentdx_version: str
    schema_version: int = SCHEMA_VERSION
    scenario_id: str | None = None
    sealed_at: str | None = None
    virtual_makespan_ms: int | None = None
    wall_makespan_ms: int | None = None
    canonical_log_hash: str | None = None
    event_count: int | None = None
    baseline_of: str | None = None
    replay_of: str | None = None
    explore_parent: str | None = None
    delay_schedule: str | None = None
    calibration_id: str | None = None
    determinism_quality: str | None = None

    @property
    def sealed(self) -> bool:
        """Return True iff the run has been sealed and refuses further appends."""
        return self.sealed_at is not None


@dataclass(frozen=True, slots=True)
class ScenarioRecord:
    """One row of the `scenarios` table (PRD §27.2)."""

    scenario_id: str
    content: str
    content_hash: str
    version: int
    path: str | None = None


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """One row of the `findings` table (PRD §27.2).

    `evidence` is stored as canonical JSON. Invariant I6 requires it to be non-empty; that
    check belongs to the verdict schema in `analysis/`, not here — the store persists what
    it is given and does not silently repair it.
    """

    finding_id: str
    run_id: str
    type: str
    severity: str
    title: str
    description: str
    evidence: Mapping[str, PayloadValue]
    analysis_version: str
    subtype: str | None = None
    recommendation: str | None = None
    suppressed_by: str | None = None
    repro_scenario_path: str | None = None


@dataclass(frozen=True, slots=True)
class ScorecardRecord:
    """One row of the `scorecards` table (PRD §27.2)."""

    run_id: str
    payload: Mapping[str, PayloadValue]
    analysis_version: str
    computed_at: str


@dataclass(frozen=True, slots=True)
class _SealSummary:
    """The facts `seal` writes into `runs`, gathered without materialising the log."""

    status: str
    sealed_at: str
    event_count: int
    virtual_makespan_ms: int | None


class Store:
    """The append-only event store for one AgentDX data file.

    Implements `events.writer.EventSink`, so an `EventWriter` can be pointed at it with no
    adapter.

    Guarantees:

    * **Append-only (I2).** No method issues an UPDATE or DELETE against `events`, and the
      database refuses both regardless of what any method does.
    * **Atomic batches.** `append` writes a whole batch in one transaction; a crash leaves
      either all of it or none of it, never half a batch.
    * **Sealed means sealed.** After `seal`, `append` for that run raises `E-STORE-005`.
    * **Round-trip fidelity.** `canonical_bytes(read_events(...)[i])` equals
      `canonical_bytes` of the event that was appended.
    * **Never validates.** Validation is `events/validators.py`; a store that re-validated
      would be a second, divergent opinion on what a valid event is.

    Not thread-safe, and deliberately so: PRD §10.2 runs the writer on one OS thread and
    PRD §24.2 puts the API in a different *process*, sharing the file through WAL rather
    than the object through a lock.
    """

    def __init__(self, conn: sqlite3.Connection, config: StoreConfig, path: Path) -> None:
        """Wrap an already-migrated connection. Prefer `Store.open`.

        Args:
            conn: A connection with the PRAGMAs applied and migrations run.
            config: Resolved store configuration; no threshold is read from anywhere else.
            path: The database file, for error messages and Parquet sibling paths.
        """
        self._conn = conn
        self._config = config
        self._path = path
        self._sealed: dict[str, str] = {}
        self._depth = 0
        """Transaction nesting depth. 0 means autocommit; see `transaction`."""

    # -- lifecycle ----------------------------------------------------------------------

    @classmethod
    def open(
        cls, path: Path, *, config: StoreConfig | None = None, allow_major_migration: bool = False
    ) -> Store:
        """Open or create a store, applying PRAGMAs and any pending minor migrations.

        Guarantees: on return the file is at `latest_version()`, is in WAL mode, and has
        both append-only triggers. A database newer than this build knows is refused rather
        than read (PRD §27.5, forward-only).

        Args:
            path: The database file. Parent directories are created.
            config: Resolved configuration; defaults to `StoreConfig()`.
            allow_major_migration: Permit a migration marked major. `agentdx migrate` passes
                True; ordinary opens do not.

        Raises:
            StoreError: `E-STORE-001` the file is not an AgentDX store.
            MigrationError: `E-STORE-002` the schema is newer than this build understands ·
                `E-STORE-008` a major migration is pending.
        """
        settings = config if config is not None else StoreConfig()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            _apply_pragmas(conn, settings)
            migrate(conn, allow_major=allow_major_migration)
        except (MigrationError, sqlite3.Error):
            conn.close()
            raise
        return cls(conn, settings, path)

    def close(self) -> None:
        """Checkpoint the WAL and close the connection.

        Guarantees: after a normal close the `-wal` file has been folded into the database,
        so copying the single file is a complete backup (PRD §27.5). A failed checkpoint is
        not fatal — the WAL is still a valid part of the database and the next open recovers
        it — so it is not allowed to mask the close. It is **not** swallowed either
        (AGENTS.md §4): it is re-raised as a warning, because a `-wal` file left beside the
        database quietly weakens §27.5's "copying the data dir is the backup" promise, and a
        user who copies only the `.db` would lose data without ever being told.
        """
        try:
            self.checkpoint()
        except StoreError as exc:
            warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
        finally:
            self._conn.close()

    def checkpoint(self) -> None:
        """Fold the WAL into the database file (PRD §27.3, §27.5).

        Guarantees: after this returns the `.db` file alone is a complete copy of the run.
        Called at `seal` and at `close`; a no-op inside an open transaction, where SQLite
        refuses to checkpoint.

        Raises:
            StoreError: `E-STORE-019` the checkpoint failed. Not fatal to correctness — the
                WAL remains a valid part of the database — but it is reported rather than
                swallowed, because it changes what "copy the file" means.
        """
        if self._depth:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            detail = (
                f"WAL checkpoint failed on {self._path} ({exc}). The database is intact, "
                f"but a `-wal` file remains beside it: copy the whole data directory, not "
                f"just the `.db`, or the copy will be missing the most recent events"
            )
            raise StoreError("E-STORE-019", detail) from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block as one atomic unit, with nested `append`/`seal` joining it.

        Without this, a multi-step write — bundle import is the only one today — commits
        each `append` separately, so a failure part-way leaves a truncated log behind a run
        row that claims to be complete. That is the exact condition NFR-13 exists to
        prevent, and the crash path avoids it only because a killed process commits nothing
        further. An interrupted *import* has no such protection unless it is one transaction.

        Guarantees: re-entrant. `append` and `seal` detect an open outer transaction and do
        not issue their own `BEGIN`/`COMMIT`, so the whole block commits once or rolls back
        entirely. On any exception the outermost level rolls back and re-raises.
        """
        self._begin()
        try:
            yield
        except Exception:
            self._rollback()
            raise
        else:
            self._commit()

    def _begin(self) -> None:
        """Open a transaction, or join the one already open."""
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth += 1

    def _commit(self) -> None:
        """Commit, but only when this is the outermost level."""
        self._depth -= 1
        if self._depth == 0:
            self._conn.commit()

    def _rollback(self) -> None:
        """Roll the whole transaction back, whatever the nesting depth."""
        self._depth = 0
        self._conn.rollback()

    def __enter__(self) -> Store:
        """Enter a context that closes the store on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the store, checkpointing the WAL."""
        self.close()

    @property
    def path(self) -> Path:
        """Return the database file backing this store."""
        return self._path

    @property
    def config(self) -> StoreConfig:
        """Return the resolved configuration this store was opened with."""
        return self._config

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the underlying connection, for the sibling modules in `store/`.

        Exposed for `snapshots.py`, `duckdb.py` and `bundle.py`, which are part of the same
        layer and share the transaction. It is not a licence for `runtime/` or `analysis/`
        to issue SQL — those layers go through the typed methods, so the schema stays a
        detail of this package.
        """
        return self._conn

    def schema_version(self) -> int:
        """Return the applied `db_version` of this database (PRD §27.5)."""
        return current_version(self._conn)

    # -- EventSink ----------------------------------------------------------------------

    def append(self, batch: Sequence[ChainedEvent]) -> None:
        """Persist a batch of chained events atomically (the `EventSink` contract).

        Guarantees: one transaction for the whole batch, so a crash mid-batch leaves the
        log at the previous batch boundary rather than part-way through one. Never modifies
        or deletes anything (I2). Snapshot rows for the batch are written in the *same*
        transaction, so a snapshot can never describe events that are not in the log.

        Raises:
            StoreError: `E-STORE-004` the run has no `runs` row · `E-STORE-005` the run is
                sealed · `E-STORE-007` the batch spans more than one run.
            sqlite3.IntegrityError: the batch replays a `(run_id, seq)` already stored,
                which the primary key refuses — a duplicate append is a writer defect.
        """
        if not batch:
            return
        run_ids = sorted({c.event.run_id for c in batch})
        if len(run_ids) != 1:
            detail = f"a batch must belong to exactly one run, got {run_ids}"
            raise StoreError("E-STORE-007", detail)
        run_id = batch[0].event.run_id
        self._require_appendable(run_id)

        rows = [_event_row(c) for c in batch]
        self._begin()
        try:
            self._conn.executemany(_INSERT_EVENT, rows)
            self._on_batch_persisted(run_id, batch)
        except Exception:
            self._rollback()
            raise
        else:
            self._commit()

    def seal(self, run_id: str, final_hash: str) -> None:
        """Mark a run complete and refuse every later append for it.

        `final_hash` is the chain head — the `this_hash` of the last event — which is what
        `EventWriter` holds. The `canonical_log_hash` of PRD §27.2 is a different quantity
        (a rolling hash over the canonical bytes, not the chain), so it is computed here
        from the stored log rather than assumed to be the same value. Two hashes that mean
        different things must not share a column.

        Guarantees: idempotent for the same `(run_id, final_hash)`; a second seal with a
        *different* hash is `E-STORE-009` rather than an overwrite, because the second
        value would silently replace recorded history.

        Raises:
            StoreError: `E-STORE-006` no such run · `E-STORE-009` already sealed with a
                different chain head.
        """
        record = self.get_run(run_id)
        if record is None:
            detail = f"cannot seal unknown run {run_id!r}"
            raise StoreError("E-STORE-006", detail)
        stored_chain = self._chain_head(run_id)
        if record.sealed:
            if stored_chain == final_hash:
                self._sealed[run_id] = stored_chain
                return
            detail = (
                f"run {run_id!r} is already sealed with chain head {stored_chain!r}; "
                f"refusing to reseal with {final_hash!r}"
            )
            raise StoreError("E-STORE-009", detail)
        log_hash = canonical_log_hash(self.read_events(run_id))
        summary = self._summarise(run_id)
        self._begin()
        try:
            self._conn.execute(
                "UPDATE runs SET status = ?, sealed_at = ?, canonical_log_hash = ?, "
                "event_count = ?, virtual_makespan_ms = ? WHERE run_id = ?",
                (
                    summary.status,
                    summary.sealed_at,
                    log_hash,
                    summary.event_count,
                    summary.virtual_makespan_ms,
                    run_id,
                ),
            )
        except Exception:
            self._rollback()
            raise
        else:
            self._commit()
        self._sealed[run_id] = final_hash
        self.checkpoint()

    def _summarise(self, run_id: str) -> _SealSummary:
        """Return the seal-time facts, read from indexed rows rather than the whole log.

        `canonical_log_hash` already streams the log; materialising it a second time here
        would put a 200 000-event run (NFR-11) fully in memory against NFR-9's 500 MB
        ceiling. `run_start` and `run_end` are single indexed lookups through
        `idx_events_type`, and the count is a `COUNT(*)`, so seal is O(1) in memory.
        """
        start = self._payload_of(run_id, EventType.RUN_START, first=True)
        end = self._payload_of(run_id, EventType.RUN_END, first=False)
        started = start.get("started_at_utc") if start else None
        wall = end.get("wall_makespan_ms") if end else None
        virtual = end.get("virtual_makespan_ms") if end else None
        status = end.get("status") if end else None
        return _SealSummary(
            status="complete" if status == "complete" else "failed",
            sealed_at=_format_sealed_at(
                started if isinstance(started, str) else "",
                wall if isinstance(wall, int) and not isinstance(wall, bool) else 0,
            ),
            event_count=self.event_count(run_id),
            virtual_makespan_ms=(
                virtual if isinstance(virtual, int) and not isinstance(virtual, bool) else None
            ),
        )

    def _payload_of(
        self, run_id: str, event_type: EventType, *, first: bool
    ) -> dict[str, PayloadValue] | None:
        """Return the payload of the first or last event of a type, or None if absent."""
        order = "ASC" if first else "DESC"
        row = self._conn.execute(
            f"SELECT payload FROM events WHERE run_id = ? AND type = ? "  # noqa: S608
            f"ORDER BY seq {order} LIMIT 1",
            (run_id, str(event_type)),
        ).fetchone()
        return None if row is None else _loads_mapping(str(row[0]), "events.payload")

    def _on_batch_persisted(self, run_id: str, batch: Sequence[ChainedEvent]) -> None:
        """Hook for in-transaction side effects; overridden by the snapshotting store.

        Kept as a no-op here so that `Store` has no dependency on `snapshots.py` and the
        two can be tested independently. `SnapshottingStore` in `snapshots.py` overrides it.
        """

    # -- runs ---------------------------------------------------------------------------

    def create_run(self, record: RunRecord) -> None:
        """Insert a `runs` row. Events for a run cannot be appended before it exists.

        Requiring the row first is deliberate: it is what lets `append` distinguish "this
        run is sealed" from "this run is unknown", and it means a bundle import cannot
        create an orphan event set whose provenance nothing records.

        Raises:
            StoreError: `E-STORE-010` a run with this id already exists.
        """
        try:
            self._conn.execute(
                "INSERT INTO runs (run_id, scenario_id, scenario_hash, graph_hash, mode, seed, "
                "status, created_at, sealed_at, virtual_makespan_ms, wall_makespan_ms, "
                "canonical_log_hash, event_count, baseline_of, replay_of, explore_parent, "
                "agentdx_version, schema_version, delay_schedule, calibration_id, "
                "determinism_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.scenario_id,
                    record.scenario_hash,
                    record.graph_hash,
                    record.mode,
                    record.seed,
                    record.status,
                    record.created_at,
                    record.sealed_at,
                    record.virtual_makespan_ms,
                    record.wall_makespan_ms,
                    record.canonical_log_hash,
                    record.event_count,
                    record.baseline_of,
                    record.replay_of,
                    record.explore_parent,
                    record.agentdx_version,
                    record.schema_version,
                    record.delay_schedule,
                    record.calibration_id,
                    record.determinism_quality,
                ),
            )
        except sqlite3.IntegrityError as exc:
            detail = f"run {record.run_id!r} already exists in {self._path}"
            raise StoreError("E-STORE-010", detail) from exc

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return the `runs` row for `run_id`, or None when there is none."""
        row = self._conn.execute(
            "SELECT run_id, scenario_id, scenario_hash, graph_hash, mode, seed, status, "
            "created_at, sealed_at, virtual_makespan_ms, wall_makespan_ms, canonical_log_hash, "
            "event_count, baseline_of, replay_of, explore_parent, agentdx_version, "
            "schema_version, delay_schedule, calibration_id, determinism_quality "
            "FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else _run_from_row(row)

    def list_runs(self) -> tuple[RunRecord, ...]:
        """Return every run, ordered by `run_id` for a stable, reproducible listing."""
        rows = self._conn.execute(
            "SELECT run_id, scenario_id, scenario_hash, graph_hash, mode, seed, status, "
            "created_at, sealed_at, virtual_makespan_ms, wall_makespan_ms, canonical_log_hash, "
            "event_count, baseline_of, replay_of, explore_parent, agentdx_version, "
            "schema_version, delay_schedule, calibration_id, determinism_quality "
            "FROM runs ORDER BY run_id"
        ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def set_run_status(self, run_id: str, status: str) -> None:
        """Update the lifecycle status of a run (PRD §6.4).

        Only `runs` is mutable; `events` is not. That asymmetry is the design: a run's
        metadata is a description of a process still happening, whereas its log is a record
        of what already did.

        Raises:
            StoreError: `E-STORE-011` the status is not one of PRD §27.2's values.
        """
        if status not in RUN_STATUSES:
            detail = f"status {status!r} is not one of {list(RUN_STATUSES)}"
            raise StoreError("E-STORE-011", detail)
        self._conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    # -- events -------------------------------------------------------------------------

    def read_events(
        self, run_id: str, *, from_seq: int = 0, to_seq: int | None = None
    ) -> Iterator[Event]:
        """Yield the events of a run in `seq` order, reconstructed from their columns.

        Guarantees: `canonical_bytes` of a yielded event equals `canonical_bytes` of the
        event that was appended. Streams rather than materialising, so a 200 000-event run
        (NFR-11) does not need to fit in memory twice.

        Args:
            run_id: The run to read.
            from_seq: First seq to include, inclusive.
            to_seq: Last seq to include, inclusive. None means "to the end".

        Raises:
            FloatNotPermittedError: a stored payload contains a float (`E-EVENT-013`),
                which means the row was written by something other than this store.
        """
        for _, event in self._iter_rows(run_id, from_seq, to_seq):
            yield event

    def read_chained(
        self, run_id: str, *, from_seq: int = 0, to_seq: int | None = None
    ) -> Iterator[ChainedEvent]:
        """Yield the events of a run with their stored chain hashes, in `seq` order.

        Guarantees: the `prev_hash`/`this_hash` returned are the values *stored*, not
        recomputed, which is what makes `verify_chain` able to detect tampering rather than
        confirm its own arithmetic.
        """
        for chained, _ in self._iter_rows(run_id, from_seq, to_seq):
            yield chained

    def event_count(self, run_id: str) -> int:
        """Return the number of events stored for a run.

        This is the quantity compared against `duckdb_threshold_events` (Q-43.2.2), so it
        counts what is *in the log*, not what a run row claims.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row[0])

    def last_seq(self, run_id: str) -> int | None:
        """Return the highest stored `seq` for a run, or None when the log is empty.

        Used to resume a WebSocket tail from `last_seq + 1` (PRD §26.2) and by the crash
        test to state how much of a killed run survived.
        """
        row = self._conn.execute(
            "SELECT MAX(seq) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row[0] is None else int(row[0])

    def verify_chain(self, run_id: str) -> int | None:
        """Return the `seq` of the first event whose stored chain entry does not verify.

        Recomputes the chain from the stored events and compares it to the stored hashes,
        so a row altered out of band — by a tool that dropped the triggers, or by a corrupt
        bundle — is located rather than merely suspected (PRD §9.7, §31.9).

        Guarantees: returns None iff every stored `prev_hash`/`this_hash` matches a fresh
        computation over the stored events, which also proves no event was inserted,
        removed or reordered. Works on a partial log: the prefix of a crashed run verifies
        on its own (NFR-13).
        """
        prev = CHAIN_GENESIS
        for chained in self.read_chained(run_id):
            expected = chain_hash(prev, chained.event)
            if chained.prev_hash != prev or chained.this_hash != expected:
                return chained.event.seq
            prev = expected
        return None

    def canonical_log_hash(self, run_id: str) -> str:
        """Return the PRD §10.7 canonical log hash of a stored run.

        Guarantees: computed from the stored events through `events.canonical`, never read
        from the `runs` row — the column is a cache of this value and comparing the two is
        how a tampered database is detected.
        """
        return canonical_log_hash(self.read_events(run_id))

    def chain_head(self, run_id: str) -> str:
        """Return the `this_hash` of the last stored event, or `CHAIN_GENESIS` if empty."""
        return self._chain_head(run_id)

    # -- findings, scorecards, scenarios ------------------------------------------------

    def upsert_scenario(self, record: ScenarioRecord) -> None:
        """Insert or replace a scenario by id.

        Replaceable because a scenario is an input the user edits, not recorded history.
        The `content_hash` is what a run pins (PRD §27.2 `runs.scenario_hash`), so editing
        a scenario cannot retroactively change what a past run was.
        """
        self._conn.execute(
            "INSERT INTO scenarios (scenario_id, path, content, content_hash, version) "
            "VALUES (?,?,?,?,?) ON CONFLICT(scenario_id) DO UPDATE SET "
            "path = excluded.path, content = excluded.content, "
            "content_hash = excluded.content_hash, version = excluded.version",
            (
                record.scenario_id,
                record.path,
                record.content,
                record.content_hash,
                record.version,
            ),
        )

    def get_scenario(self, scenario_id: str) -> ScenarioRecord | None:
        """Return a scenario by id, or None."""
        row = self._conn.execute(
            "SELECT scenario_id, path, content, content_hash, version FROM scenarios "
            "WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()
        if row is None:
            return None
        return ScenarioRecord(
            scenario_id=str(row[0]),
            path=None if row[1] is None else str(row[1]),
            content=str(row[2]),
            content_hash=str(row[3]),
            version=int(row[4]),
        )

    def upsert_finding(self, record: FindingRecord) -> None:
        """Insert or replace a finding by id.

        Findings are *derived* from the log and are regenerated whenever the analysis
        version changes, so unlike events they are replaceable. The log they cite is not.
        """
        self._conn.execute(
            "INSERT INTO findings (finding_id, run_id, type, subtype, severity, title, "
            "description, evidence, recommendation, suppressed_by, repro_scenario_path, "
            "analysis_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(finding_id) DO UPDATE SET "
            "type = excluded.type, subtype = excluded.subtype, severity = excluded.severity, "
            "title = excluded.title, description = excluded.description, "
            "evidence = excluded.evidence, recommendation = excluded.recommendation, "
            "suppressed_by = excluded.suppressed_by, "
            "repro_scenario_path = excluded.repro_scenario_path, "
            "analysis_version = excluded.analysis_version",
            (
                record.finding_id,
                record.run_id,
                record.type,
                record.subtype,
                record.severity,
                record.title,
                record.description,
                encode_value(dict(record.evidence)),
                record.recommendation,
                record.suppressed_by,
                record.repro_scenario_path,
                record.analysis_version,
            ),
        )

    def list_findings(self, run_id: str) -> tuple[FindingRecord, ...]:
        """Return the findings of a run, ordered by `finding_id` for a stable listing."""
        rows = self._conn.execute(
            "SELECT finding_id, run_id, type, subtype, severity, title, description, "
            "evidence, recommendation, suppressed_by, repro_scenario_path, analysis_version "
            "FROM findings WHERE run_id = ? ORDER BY finding_id",
            (run_id,),
        ).fetchall()
        return tuple(
            FindingRecord(
                finding_id=str(r[0]),
                run_id=str(r[1]),
                type=str(r[2]),
                subtype=None if r[3] is None else str(r[3]),
                severity=str(r[4]),
                title=str(r[5]),
                description=str(r[6]),
                evidence=_loads_mapping(str(r[7]), "findings.evidence"),
                recommendation=None if r[8] is None else str(r[8]),
                suppressed_by=None if r[9] is None else str(r[9]),
                repro_scenario_path=None if r[10] is None else str(r[10]),
                analysis_version=str(r[11]),
            )
            for r in rows
        )

    def upsert_scorecard(self, record: ScorecardRecord) -> None:
        """Insert or replace the scorecard of a run."""
        self._conn.execute(
            "INSERT INTO scorecards (run_id, payload, analysis_version, computed_at) "
            "VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET payload = excluded.payload, "
            "analysis_version = excluded.analysis_version, computed_at = excluded.computed_at",
            (
                record.run_id,
                encode_value(dict(record.payload)),
                record.analysis_version,
                record.computed_at,
            ),
        )

    def get_scorecard(self, run_id: str) -> ScorecardRecord | None:
        """Return the scorecard of a run, or None."""
        row = self._conn.execute(
            "SELECT run_id, payload, analysis_version, computed_at FROM scorecards "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return ScorecardRecord(
            run_id=str(row[0]),
            payload=_loads_mapping(str(row[1]), "scorecards.payload"),
            analysis_version=str(row[2]),
            computed_at=str(row[3]),
        )

    # -- internals ----------------------------------------------------------------------

    def _require_appendable(self, run_id: str) -> None:
        """Raise unless `run_id` names a known, unsealed run.

        Raises:
            StoreError: `E-STORE-004` unknown run · `E-STORE-005` the run is sealed.
        """
        if run_id in self._sealed:
            detail = f"run {run_id!r} is sealed; the log is append-only and closed (I2)"
            raise StoreError("E-STORE-005", detail)
        row = self._conn.execute(
            "SELECT sealed_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            detail = (
                f"no runs row for {run_id!r}; call create_run before appending events so "
                f"the log has recorded provenance"
            )
            raise StoreError("E-STORE-004", detail)
        if row[0] is not None:
            self._sealed[run_id] = self._chain_head(run_id)
            detail = f"run {run_id!r} is sealed; the log is append-only and closed (I2)"
            raise StoreError("E-STORE-005", detail)

    def _chain_head(self, run_id: str) -> str:
        """Return the stored `this_hash` of the highest-seq event, or the genesis value."""
        row = self._conn.execute(
            "SELECT this_hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return CHAIN_GENESIS if row is None or row[0] is None else str(row[0])

    def _iter_rows(
        self, run_id: str, from_seq: int, to_seq: int | None
    ) -> Iterator[tuple[ChainedEvent, Event]]:
        """Yield `(ChainedEvent, Event)` for the requested seq window, in seq order."""
        if to_seq is None:
            sql = f"{_SELECT_EVENT} WHERE run_id = ? AND seq >= ? ORDER BY seq"
            params: tuple[str | int, ...] = (run_id, from_seq)
        else:
            sql = f"{_SELECT_EVENT} WHERE run_id = ? AND seq >= ? AND seq <= ? ORDER BY seq"
            params = (run_id, from_seq, to_seq)
        cursor = self._conn.execute(sql, params)
        while True:
            rows = cursor.fetchmany(1024)
            if not rows:
                return
            for row in rows:
                chained = _chained_from_row(row)
                yield chained, chained.event


# ---------------------------------------------------------------------------------------
# Row <-> Event conversion
# ---------------------------------------------------------------------------------------


def _apply_pragmas(conn: sqlite3.Connection, config: StoreConfig) -> None:
    """Apply the PRD §27.2 / §27.3 connection settings.

    Guarantees: WAL is set on the file (persistent) and verified, because a silent failure
    to enter WAL mode would remove the concurrent-reader property PRD §24.2 depends on and
    would only surface as an API server that blocks during a run. `synchronous` comes from
    configuration and has already been checked against SQLite's accepted values, so the
    interpolation below cannot carry arbitrary text.

    Raises:
        StoreError: `E-STORE-012` the database refused WAL mode.
    """
    conn.execute("PRAGMA foreign_keys=ON")
    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    mode = "" if row is None else str(row[0]).lower()
    if mode != "wal":
        detail = (
            f"the database refused WAL mode (journal_mode={mode!r}). WAL is what lets the "
            f"API read a run while the runner writes it (PRD §24.2, §27.3); without it the "
            f"live view would block the run"
        )
        raise StoreError("E-STORE-012", detail)
    conn.execute(f"PRAGMA synchronous={config.synchronous}")


def _event_row(chained: ChainedEvent) -> tuple[str | int | None, ...]:
    """Return the `events` row tuple for a chained event, in `_EVENT_COLUMNS` order.

    Guarantees: the three composite columns are encoded with `canonical.encode_value`, so
    the bytes stored are the canonical bytes and no second serialiser exists in this
    codebase (CONTEXT.md §14, fact ④).
    """
    event = chained.event
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
        chained.prev_hash,
        chained.this_hash,
    )


def _chained_from_row(row: Sequence[object]) -> ChainedEvent:
    """Return the `ChainedEvent` a stored row represents.

    Guarantees: the reconstructed event's canonical bytes equal those of the event that was
    written, which is what makes `verify_chain` meaningful over stored rows.

    Raises:
        FloatNotPermittedError: a composite column contains a float (`E-EVENT-013`).
    """
    event = Event(
        schema_version=int(_as_int(row[2])),
        run_id=str(row[0]),
        seq=int(_as_int(row[1])),
        sched_step=int(_as_int(row[3])),
        virtual_ts_ms=int(_as_int(row[4])),
        wall_ts_ms=int(_as_int(row[5])),
        vclock=_loads_vclock(str(row[10])),
        type=EventType(str(row[8])),
        causal_parents=_loads_parents(str(row[11])),
        payload=_loads_mapping(str(row[13]), "events.payload"),
        agent_id=None if row[6] is None else str(row[6]),
        clock_slot=None if row[7] is None else str(row[7]),
        span_id=None if row[9] is None else str(row[9]),
        fault_id=None if row[12] is None else str(row[12]),
    )
    return ChainedEvent(
        event=event,
        prev_hash=CHAIN_GENESIS if row[14] is None else str(row[14]),
        this_hash="" if row[15] is None else str(row[15]),
    )


def _run_from_row(row: Sequence[object]) -> RunRecord:
    """Return the `RunRecord` a `runs` row represents, in DDL column order."""
    return RunRecord(
        run_id=str(row[0]),
        scenario_id=None if row[1] is None else str(row[1]),
        scenario_hash=str(row[2]),
        graph_hash=str(row[3]),
        mode=str(row[4]),
        seed=int(_as_int(row[5])),
        status=str(row[6]),
        created_at=str(row[7]),
        sealed_at=None if row[8] is None else str(row[8]),
        virtual_makespan_ms=None if row[9] is None else int(_as_int(row[9])),
        wall_makespan_ms=None if row[10] is None else int(_as_int(row[10])),
        canonical_log_hash=None if row[11] is None else str(row[11]),
        event_count=None if row[12] is None else int(_as_int(row[12])),
        baseline_of=None if row[13] is None else str(row[13]),
        replay_of=None if row[14] is None else str(row[14]),
        explore_parent=None if row[15] is None else str(row[15]),
        agentdx_version=str(row[16]),
        schema_version=int(_as_int(row[17])),
        delay_schedule=None if row[18] is None else str(row[18]),
        calibration_id=None if row[19] is None else str(row[19]),
        determinism_quality=None if row[20] is None else str(row[20]),
    )


def _as_int(value: object) -> int:
    """Return an integer column value, refusing a float.

    Raises:
        FloatNotPermittedError: the column holds a float, which means it was not written by
            this store (`E-EVENT-013`, ruling R4).
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        where = "an integer column"
        raise FloatNotPermittedError(where)
    if isinstance(value, int):
        return value
    return int(str(value))


def _reject_float(text: str) -> int:
    """Reject any JSON number that is not an integer (ruling R4)."""
    raise FloatNotPermittedError(text)


def _loads_mapping(text: str, where: str) -> dict[str, PayloadValue]:
    """Return a stored canonical-JSON object as a payload mapping.

    Raises:
        FloatNotPermittedError: the stored text contains a float (`E-EVENT-013`).
        StoreError: `E-STORE-013` the column does not hold a JSON object.
    """
    parsed: object = json.loads(text, parse_float=_reject_float)
    if not isinstance(parsed, Mapping):
        detail = f"{where} does not hold a JSON object"
        raise StoreError("E-STORE-013", detail)
    return {str(k): _as_payload(v) for k, v in parsed.items()}


def _loads_vclock(text: str) -> dict[str, int]:
    """Return a stored canonical-JSON vector clock.

    Raises:
        StoreError: `E-STORE-013` the column does not hold an object of integers.
    """
    parsed: object = json.loads(text, parse_float=_reject_float)
    if not isinstance(parsed, Mapping):
        detail = "events.vclock does not hold a JSON object"
        raise StoreError("E-STORE-013", detail)
    return {str(k): _as_int(v) for k, v in parsed.items()}


def _loads_parents(text: str) -> list[int]:
    """Return a stored canonical-JSON `causal_parents` array.

    Raises:
        StoreError: `E-STORE-013` the column does not hold an array of integers.
    """
    parsed: object = json.loads(text, parse_float=_reject_float)
    if not isinstance(parsed, list):
        detail = "events.causal_parents does not hold a JSON array"
        raise StoreError("E-STORE-013", detail)
    return [_as_int(v) for v in parsed]


def _as_payload(value: object) -> PayloadValue:
    """Return `value` typed as a `PayloadValue`, refusing anything outside the contract.

    Raises:
        FloatNotPermittedError: a float appeared (`E-EVENT-013`).
        StoreError: `E-STORE-013` a value outside `PayloadValue` appeared.
    """
    if isinstance(value, float):
        # Checked before the scalar branch, not inside it: `isinstance(True, int)` is True
        # and a float is neither, so testing floats first is what keeps the scalar branch
        # from having an unreachable arm that a type checker would (correctly) flag.
        where = "a payload value"
        raise FloatNotPermittedError(where)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, list):
        return [_as_payload(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _as_payload(v) for k, v in value.items()}
    detail = f"a stored value has type {type(value).__name__}"
    raise StoreError("E-STORE-013", detail)


def _format_sealed_at(started_at_utc: str, wall_makespan_ms: int) -> str:
    """Return the seal timestamp, derived from the log rather than from a clock.

    The store does not read the wall clock (AGENTS.md §4.1). `run_start.payload.started_at_utc`
    plus the run's wall makespan is the log's own account of when it ended, so a bundle
    sealed on import records the *original* run's time rather than the importer's.

    Known limitation, recorded rather than hidden: the result is not ISO-8601. P14's API
    will want a real timestamp and should overturn this rather than parse it.
    """
    return f"{started_at_utc}+{wall_makespan_ms}ms" if started_at_utc else f"+{wall_makespan_ms}ms"


def latest_schema_version() -> int:
    """Return the store schema version this build produces (PRD §27.5)."""
    return latest_version()


__all__ = [
    "APPEND_ONLY_MESSAGE",
    "RUN_STATUSES",
    "FindingRecord",
    "RunRecord",
    "ScenarioRecord",
    "ScorecardRecord",
    "Store",
    "StoreError",
    "latest_schema_version",
]
