"""Migrations are forward-only, versioned, and refuse a schema newer than the code knows.

Design constraint 7. The refusal is the part that matters: a build that silently reads a
database written by a newer build cannot know whether the column it is ignoring changes an
analysis result, and "it seemed to work" is how a stale binary publishes wrong findings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentdx.config import StoreConfig
from agentdx.store.migrations import (
    DB_VERSION_KEY,
    MIGRATIONS,
    Migration,
    MigrationError,
    current_version,
    latest_version,
    migrate,
    pending,
    trigger_names,
)
from agentdx.store.migrations._base import Migration as BaseMigration
from agentdx.store.sqlite import Store


def test_registry_is_sequential_from_one() -> None:
    """Versions are exactly 1..N in order, checked at import time."""
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    assert latest_version() == len(MIGRATIONS)


def test_open_applies_migrations_and_records_the_version(tmp_path: Path) -> None:
    """A fresh file is migrated to the latest version on open."""
    with Store.open(tmp_path / "agentdx.db") as store:
        assert store.schema_version() == latest_version()
        row = store.connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (DB_VERSION_KEY,)
        ).fetchone()
        assert int(row[0]) == latest_version()


def test_reopening_applies_nothing(tmp_path: Path) -> None:
    """Migration is idempotent: the second open has nothing pending."""
    path = tmp_path / "agentdx.db"
    Store.open(path).close()
    with Store.open(path) as store:
        assert pending(store.connection) == ()


def test_a_newer_database_is_refused(tmp_path: Path) -> None:
    """`E-STORE-002`: a `db_version` above what this build knows is refused, not read.

    Forward-only means exactly this. The alternative — best-effort reading — is the failure
    mode that produces analysis output nobody can trust.
    """
    path = tmp_path / "agentdx.db"
    Store.open(path).close()
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = ?",
        (str(latest_version() + 7), DB_VERSION_KEY),
    )
    conn.close()

    with pytest.raises(MigrationError) as excinfo:
        Store.open(path)
    assert excinfo.value.code == "E-STORE-002"
    assert "forward-only" in str(excinfo.value)


def test_a_database_with_events_but_no_schema_meta_is_refused(tmp_path: Path) -> None:
    """`E-STORE-001`: a foreign database is not mistaken for a fresh one.

    Without this check, a file with an unrelated `events` table would report version 0 and
    the initial migration would run against it, producing a corrupt hybrid.
    """
    path = tmp_path / "foreign.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("CREATE TABLE events (seq INTEGER)")
    conn.close()

    with pytest.raises(MigrationError) as excinfo:
        Store.open(path)
    assert excinfo.value.code == "E-STORE-001"


def test_a_major_migration_does_not_run_on_open(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """`E-STORE-008`: a major migration requires `agentdx migrate` (PRD §27.5).

    The message names the command and the backup, because an error that stops a user
    without telling them what to do next is only half an error message (AGENTS.md §4).
    """
    path = tmp_path / "agentdx.db"
    Store.open(path).close()

    major = BaseMigration(
        version=latest_version() + 1,
        name="a major change",
        statements=("ALTER TABLE runs ADD COLUMN hypothetical TEXT",),
        major=True,
    )
    monkeypatch.setattr("agentdx.store.migrations.MIGRATIONS", (*MIGRATIONS, major))

    with pytest.raises(MigrationError) as excinfo:
        Store.open(path)
    assert excinfo.value.code == "E-STORE-008"
    assert "agentdx migrate" in str(excinfo.value)

    with Store.open(path, allow_major_migration=True) as store:
        assert store.schema_version() == major.version


def test_a_migration_that_drops_the_triggers_is_refused(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """`E-STORE-006`: a migration may not leave the append-only triggers missing.

    This is the interaction design constraint 1 and constraint 7 have with each other. The
    triggers are what make I2 a database property, so a migration that removed them and
    forgot to reinstate them would silently downgrade the invariant to a convention. The
    runner checks before committing, so the migration is rolled back rather than applied.
    """
    path = tmp_path / "agentdx.db"
    Store.open(path).close()
    version_before = latest_version()

    bad = BaseMigration(
        version=version_before + 1,
        name="drops a trigger and forgets it",
        statements=("DROP TRIGGER events_no_update",),
    )
    monkeypatch.setattr("agentdx.store.migrations.MIGRATIONS", (*MIGRATIONS, bad))

    with pytest.raises(MigrationError) as excinfo:
        Store.open(path)
    assert excinfo.value.code == "E-STORE-006"

    conn = sqlite3.connect(str(path))
    try:
        assert int(conn.execute("SELECT value FROM schema_meta").fetchone()[0]) == version_before
        present = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        }
        assert set(trigger_names()) <= present, "the failed migration was not rolled back"
    finally:
        conn.close()


def test_a_rewriting_migration_reinstates_the_triggers(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """A migration that declares `rewrites_events` may touch events, and is put back safely.

    Declaring the flag is a visible line in a diff; the runner drops the triggers, applies
    the migration and reinstates them inside one transaction. This is the only sanctioned
    route past I2 and it exists because forbidding it outright would make a required
    schema change impossible rather than merely careful.
    """
    path = tmp_path / "agentdx.db"
    Store.open(path).close()

    rewriting = BaseMigration(
        version=latest_version() + 1,
        name="a rewriting migration",
        statements=("UPDATE events SET fault_id = fault_id",),
        rewrites_events=True,
    )
    monkeypatch.setattr("agentdx.store.migrations.MIGRATIONS", (*MIGRATIONS, rewriting))

    with Store.open(path) as store:
        assert store.schema_version() == rewriting.version
        present = {
            str(r[0])
            for r in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert set(trigger_names()) <= present


def test_the_initial_migration_creates_every_prd_table(tmp_path: Path) -> None:
    """Migration 1 creates exactly the PRD §27.2 tables."""
    with Store.open(tmp_path / "agentdx.db") as store:
        tables = {
            str(r[0])
            for r in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "runs",
        "events",
        "findings",
        "scorecards",
        "state_snapshots",
        "scenarios",
        "schema_meta",
    } <= tables


def test_wal_mode_and_pragmas_are_applied(tmp_path: Path) -> None:
    """WAL is on the file, and `synchronous` comes from configuration.

    WAL is what lets the API process read a run while the runner writes it (PRD §24.2);
    without it the live view would block the run, and the symptom would look like a
    scheduler problem rather than a storage one.
    """
    config = StoreConfig(synchronous="FULL")
    with Store.open(tmp_path / "agentdx.db", config=config) as store:
        assert str(store.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(store.connection.execute("PRAGMA synchronous").fetchone()[0]) == 2


def test_migration_type_is_shared_between_runner_and_modules() -> None:
    """The runner's `Migration` is the one numbered migrations are instances of."""
    assert Migration is BaseMigration
    assert all(isinstance(m, Migration) for m in MIGRATIONS)


def test_current_version_of_an_empty_file_is_zero(tmp_path: Path) -> None:
    """0 means "never migrated", which is distinguishable from version 1."""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert current_version(conn) == 0
        assert len(migrate(conn)) == latest_version()
        assert current_version(conn) == latest_version()
    finally:
        conn.close()
