"""Persistence: SQLite (WAL), DuckDB views, migrations, snapshots and bundles. No analysis logic.

Append-only immutability is enforced by SQLite triggers, not by convention (I2).
May import `events`; must not import `runtime`, `sdk` or `analysis` (CONTEXT.md §4).
Will contain: sqlite.py, duckdb.py, snapshots.py, bundle.py (P03).
"""
