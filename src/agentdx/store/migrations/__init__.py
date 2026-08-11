"""Forward-only, versioned schema migrations and the runner that applies them (PRD §27.5).

Four properties, each of which exists because its absence is a data-loss bug:

1. **Forward only.** There is no `down`. A down-migration over an append-only log is a
   contradiction — it would have to delete or rewrite events, which the triggers of
   invariant I2 refuse. Reversal is a restore from the data directory (PRD §27.5 "Backup").
2. **Refuses a newer database than the code knows.** Opening a `db_version` above
   `latest_version()` is `E-STORE-002`, never a best-effort read. A build that does not
   know a column cannot know whether ignoring it changes an analysis result, and quietly
   reading a future database is how a stale binary publishes wrong findings.
3. **Sequential, no gaps.** Versions are 1..N with no holes, asserted at import time rather
   than discovered when a migration is skipped on a user's machine.
4. **Minor migrations run on open; major ones require `agentdx migrate`.** PRD §27.5. A
   major migration is one whose result an older build could not read back.

**The triggers.** `events_no_update` / `events_no_delete` make the events table
append-only *in the database* (I2), which also means a migration cannot rewrite an event
row. That is the intended constraint, not an obstacle to route around: a migration that
needs to change existing events is changing recorded history. The runner therefore refuses
to apply a migration whose statements touch `events` rows unless the migration declares
`rewrites_events=True`, in which case the runner drops the two triggers, applies it, and
reinstates and *verifies* them inside the same transaction. Declaring the flag is a visible
line in a diff; silently dropping a trigger inside a migration script would not be.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Final

from agentdx.store.migrations._base import Migration
from agentdx.store.migrations.m0001_initial import MIGRATION as M0001

_DOCS: Final = "docs/storage.md"

DB_VERSION_KEY: Final = "db_version"
"""The `schema_meta` key holding the applied migration version (PRD §27.5)."""

_TRIGGER_NAMES: Final = ("events_no_update", "events_no_delete")


class MigrationError(RuntimeError):
    """A migration could not be applied, or the database is not safe to open.

    Guarantees: carries a stable `E-STORE-NNN` code so the CLI and the bundle importer
    branch on the same code space, and never leaves a half-applied migration — every
    migration is applied inside one transaction.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from its stable code and a human-readable explanation."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


MIGRATIONS: Final[tuple[Migration, ...]] = (M0001,)
"""Every known migration, in ascending version order. Append only."""


def _check_registry() -> None:
    """Assert the registry is 1..N with no gaps and no duplicates.

    Guarantees: runs at import time, so a mis-numbered migration fails the build rather
    than the first user upgrade that skips it.

    Raises:
        MigrationError: `E-STORE-003` the versions are not exactly 1..N in order.
    """
    versions = [m.version for m in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            "E-STORE-003",
            f"migration versions must be exactly 1..N in ascending order, got {versions}",
        )


_check_registry()


def latest_version() -> int:
    """Return the highest schema version this build knows how to produce.

    Guarantees: equal to `len(MIGRATIONS)` by the registry check above, so it cannot drift
    from the registry.
    """
    return MIGRATIONS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    """Return the `db_version` recorded in the database, or 0 for an empty one.

    Guarantees: 0 means "no migration has ever been applied", which is distinguishable
    from 1 — an important distinction, because a database with tables but no
    `schema_meta` row is a corrupted or foreign database, not a fresh one.

    Raises:
        MigrationError: `E-STORE-001` the file has AgentDX tables but no version row, so
            its schema cannot be identified.
    """
    if not _table_exists(conn, "schema_meta"):
        if _table_exists(conn, "events"):
            raise MigrationError(
                "E-STORE-001",
                "the database has an `events` table but no `schema_meta`; it is not an "
                "AgentDX store, or it was written by a tool that bypassed the migrations",
            )
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (DB_VERSION_KEY,)).fetchone()
    if row is None:
        raise MigrationError(
            "E-STORE-001", f"`schema_meta` exists but holds no {DB_VERSION_KEY!r} row"
        )
    return int(row[0])


def pending(conn: sqlite3.Connection) -> tuple[Migration, ...]:
    """Return the migrations that have not yet been applied, in order.

    Guarantees: empty iff the database is already at `latest_version()`. Does not modify
    the database.

    Raises:
        MigrationError: `E-STORE-002` the database is newer than this build knows.
    """
    version = current_version(conn)
    if version > latest_version():
        raise MigrationError(
            "E-STORE-002",
            f"database schema version {version} is newer than this build understands "
            f"(highest known: {latest_version()}). Migrations are forward-only and this "
            f"build will not guess at a schema it does not know. Upgrade AgentDX",
        )
    return tuple(m for m in MIGRATIONS if m.version > version)


def migrate(conn: sqlite3.Connection, *, allow_major: bool = False) -> tuple[int, ...]:
    """Apply every pending migration and return the versions applied, in order.

    Each migration runs inside its own transaction together with the `db_version` update,
    so an interrupted upgrade leaves the database at a version that is actually true.

    Args:
        conn: An open connection. Left open, and at the new version on return.
        allow_major: Permit migrations marked `major`. `Store.open` passes False, so a
            major migration surfaces as a refusal naming `agentdx migrate` rather than as
            an automatic rewrite of the user's data (PRD §27.5).

    Returns:
        The versions applied, ascending. Empty when the database was already current.

    Raises:
        MigrationError: `E-STORE-002` the database is newer than this build knows ·
            `E-STORE-008` a major migration is pending and `allow_major` is False ·
            `E-STORE-006` a migration declared `rewrites_events=False` yet the append-only
            triggers were missing afterwards.
    """
    applied: list[int] = []
    for migration in pending(conn):
        if migration.major and not allow_major:
            raise MigrationError(
                "E-STORE-008",
                f"migration {migration.version} ({migration.name}) is a major schema "
                f"change and does not run automatically on open. Run `agentdx migrate` "
                f"after backing up the data directory (it is a directory of files; "
                f"copying it is the backup, PRD §27.5)",
            )
        _apply(conn, migration)
        applied.append(migration.version)
    return tuple(applied)


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration atomically, managing the append-only triggers around it.

    Guarantees: on return the database is at `migration.version` and both append-only
    triggers exist. On any failure the transaction is rolled back and the recorded version
    is unchanged, so the database is never at a version whose statements only half ran.

    Raises:
        MigrationError: `E-STORE-006` the triggers were absent after the migration.
        sqlite3.Error: propagated after rollback; a migration that cannot run is a defect
            in this build, not a condition to recover from.
    """
    had_events = _table_exists(conn, "events")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if migration.rewrites_events and had_events:
            for name in _TRIGGER_NAMES:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for statement in migration.statements:
            conn.execute(statement)
        if migration.rewrites_events and had_events:
            for statement in M0001.triggers:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (DB_VERSION_KEY, str(migration.version)),
        )
        _require_triggers(conn, migration)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _require_triggers(conn: sqlite3.Connection, migration: Migration) -> None:
    """Refuse to commit a migration that left the append-only triggers missing.

    Extracted from `_apply` so the raise happens outside the transaction's `try`, and so the
    check reads as what it is: the post-condition every migration must satisfy, checked
    before the commit rather than discovered by the next writer.

    Raises:
        MigrationError: `E-STORE-006` one or both triggers are absent.
    """
    missing = [n for n in _TRIGGER_NAMES if not _trigger_exists(conn, n)]
    if missing:
        detail = (
            f"migration {migration.version} ({migration.name}) left the append-only "
            f"triggers {missing} missing. The events table must be append-only in the "
            f"database at all times (invariant I2); application discipline is not enforcement"
        )
        raise MigrationError("E-STORE-006", detail)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Return True iff a table of this name exists in the main database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _trigger_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Return True iff a trigger of this name exists in the main database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def trigger_names() -> tuple[str, ...]:
    """Return the append-only trigger names, for tests and `agentdx doctor`.

    Guarantees: this tuple is what `_apply` verifies after every migration, so a test that
    asserts against it is asserting against the thing actually enforced.
    """
    return _TRIGGER_NAMES


def applied_history(conn: sqlite3.Connection) -> Sequence[int]:
    """Return the single applied version as a one-element sequence, or empty.

    PRD §27.5 records only `db_version`, not a migration history table. This accessor
    exists so callers do not read `schema_meta` directly and so a future history table is
    a change in one place.
    """
    version = current_version(conn)
    return () if version == 0 else (version,)


__all__ = [
    "DB_VERSION_KEY",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "applied_history",
    "current_version",
    "latest_version",
    "migrate",
    "pending",
    "trigger_names",
]
