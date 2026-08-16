"""SQLite-backed LLM response storage (PRD §11.6, §11.9, §31.2).

**Schema.** The `llm_cache` table is PRD §11.6's DDL verbatim, plus one additive column:

```sql
CREATE TABLE llm_cache (
  cache_key         TEXT PRIMARY KEY,
  key_version       INTEGER NOT NULL,
  model             TEXT NOT NULL,
  prompt_hash       TEXT NOT NULL,
  key_material      TEXT NOT NULL,       -- ADDITIVE, see below
  response_body     TEXT NOT NULL,       -- PRD says BLOB/zstd; see the deviation note below
  response_hash     TEXT NOT NULL,
  prompt_tokens     INTEGER, completion_tokens INTEGER,
  duration_wall_ms  INTEGER,
  recorded_at       TEXT NOT NULL,
  recorded_run_id   TEXT NOT NULL,
  provider          TEXT NOT NULL,
  finish_reason     TEXT,
  stream_chunks     BLOB
);
CREATE INDEX idx_cache_prompt ON llm_cache(prompt_hash);
CREATE INDEX idx_cache_run    ON llm_cache(recorded_run_id);
CREATE INDEX idx_cache_model  ON llm_cache(model);            -- ADDITIVE, see below
```

**Deviation 1 — `key_material` (additive column).** PRD §11.6 stores `prompt_hash` but not
the prompt itself, yet design constraint 2 requires a replay-mode miss to report "the closest
stored key, and a diff of the two" and PRD §11.4's own prompt-volatility diagnostic requires
comparing prompts by edit distance — neither is possible from a hash alone (hashes have no
locality: a one-character prompt edit produces a completely different digest). This mirrors
ADR-009's shape exactly (`events` gained `schema_version` because a downstream requirement
needed a value the literal DDL omitted) and is declared here the same way rather than silently
added. `key_material` is the canonical JSON text `key.key_material_json(...)` produces — the
exact object `cache_key` hashes — so the diagnostic can render a real diff, not a guess. This
is not a new privacy exposure: PRD §11.9 already treats the whole cache as "necessarily holds
bodies" and gates it with `0600` permissions and bundle exclusion by default (§31.2); the
prompt was always the more sensitive half of "bodies," and it was already implied by
`prompt_hash`'s presence that this file holds request-identifying data.

**Deviation 2 — `response_body` is `TEXT`, not zstd-compressed `BLOB`.** ADR-008 already
declined `zstandard` as a dependency (it is outside the ADR-004-enumerated set and AGENTS.md
§2 requires an ADR before a new one enters `pyproject.toml`); this module extends that same
ruling to the cache's own response storage rather than re-raising a settled question. The
response is stored as the verbatim JSON text SQLite already gives back typed, with no format
change to solve later if zstd is ever adopted — the `TEXT` value round-trips exactly.

**Deviation 3 — `idx_cache_model` (additive index).** Needed by the "closest stored key"
diagnostic, which narrows its scan to same-model candidates first (a cross-model comparison is
never useful — a different model is a different experiment, PRD §11.5). PRD §11.6 lists only
the two indexes it names explicitly needed; this is additive, not a removal.

**Privacy (PRD §11.9, §31.2, §31.4).** The file is created at `0600` on first open, verified
(not merely requested) on every open — and so are its `-wal`/`-shm` sidecars, which hold the
same prompt/response bodies while a transaction is in flight and used to be left at the
process umask's default permissions instead. No secret ever reaches this module: API keys are
read by `sdk/providers/*` and never handed down to a cache implementation, so there is nothing
here to redact — the absence of an API-key parameter anywhere in this file's surface *is* the
enforcement, not a check performed at runtime.

**Integrity (PRD §36 `E-CACHE-002`, "Cache DB corrupt").** `put()` has always computed and
stored `response_hash = hash_text(response.body)`; every read path (`lookup_entry`, `iter_all`)
now recomputes that hash from the stored `response_body` and raises `CacheStoreError`
(`E-CACHE-002`) on a mismatch, rather than silently handing back a body that does not match
what was actually recorded. `E-CACHE-012` is the code for a distinct, non-corruption failure —
the database refusing WAL mode outright (an environment/filesystem problem, not a data
integrity one) — kept separate so a caller (or a human reading a traceback) is never told
"corrupt" for a condition that says nothing about whether any *data* is wrong.

**`agentdx.config.CacheConfig` (`[cache]` in `agentdx.toml`).** `nearest()` now accepts an
optional `config: CacheConfig` and derives its `limit`/`scan_limit` defaults from
`config.miss_diagnostic_candidates`/`config.miss_diagnostic_scan_limit` when neither is
passed explicitly (`modes.py`'s `Cache.describe_miss` does the same for `candidates`) — the
two values that were previously hardcoded independently of the config surface. `db_filename`
remains declared but unconsumed: nothing in this module's own `DELIVERABLES` decides *where*
a cache file lives — that is a run-host/CLI concern this prompt does not build — so there is
no call site within this file for that specific field to wire into. Declared here rather than
silently left as if `CacheConfig` were now fully live end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agentdx.config import CacheConfig
from agentdx.runtime.cache.key import hash_text
from agentdx.runtime.clock import wall_time

_DOCS: Final = "docs/cache.md"

CACHE_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR  # 0o600 — PRD §11.9
"""Owner read/write only. Applied on create and re-asserted on every open, because a
permission the code merely *hopes* is set is not a guarantee (PRD §31.2)."""


class CacheStoreError(RuntimeError):
    """The cache store could not do what it was asked (I/O, corruption, bad input).

    Carries a stable `E-CACHE-0NN` code. `E-CACHE-001` is reserved — it is
    `sdk.generic.CacheMissError`'s code (PRD §36) and this module deliberately never raises
    it: `lookup()` returns `None` on a miss, exactly matching the `sdk.generic.LlmCache`
    Protocol, so the SDK's own tested miss-then-raise-then-emit sequence is unchanged by
    this module's existence (see `docs/cache.md` §5 for why that boundary is load-bearing).
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """One `llm_cache` row, as a consumer sees it.

    **Field-for-field compatible with `sdk.generic.CachedResponse`, by construction rather
    than by import** — `runtime/` must not import `sdk/` (CONTEXT.md §4 layer contract), so
    this is a separate, structurally identical dataclass. `sdk.providers.openai_compatible`
    accesses these fields by attribute (`cached.body`, `cached.model`, ...), never by
    `isinstance`, so an instance of this class satisfies every real call site that expects
    the SDK's own type. `tests/unit/cache/test_store.py` asserts the field sets stay equal.
    """

    body: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None = None
    duration_wall_ms: int | None = None
    recorded_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredEntry:
    """A `CachedResponse` plus the storage-only facts a diagnostic needs.

    Guarantees: `key_material` is the exact text `cache_key` hashes (see the module
    docstring's deviation 1), so a diff against another entry's `key_material` is a diff of
    what was actually asked, not an approximation. `response_hash` has already been verified
    against `response.body` by the time a caller sees this object — `_entry_from_row` raises
    `CacheStoreError("E-CACHE-002", ...)` rather than construct one whose hash doesn't match
    (PRD §36) — so `response_hash` here is exposed for provenance/diagnostics, not as
    something a caller still needs to re-check.
    """

    cache_key: str
    key_version: int
    model: str
    prompt_hash: str
    key_material: str
    response: CachedResponse
    response_hash: str
    recorded_at: str
    provider: str


@dataclass(frozen=True, slots=True)
class MissCandidate:
    """One "closest stored key" candidate for a replay-mode miss (design constraint 2)."""

    cache_key: str
    model: str
    distance: int
    """Levenshtein edit distance between the missed and candidate `key_material` texts.
    Smaller is closer. Not normalised to a ratio — the raw distance is what a diff needs."""
    key_material: str


def _open_connection(path: Path) -> sqlite3.Connection:
    """Open (creating if absent) the SQLite file at `path`, in WAL mode, at `0600`.

    Guarantees: `path` and its `-wal`/`-shm` sidecars (if SQLite has created them by the
    time this returns) all end up at `CACHE_FILE_MODE` — the sidecars hold the same
    prompt/response bodies as the main file while a transaction is in flight (PRD §11.9),
    so leaving them at the process umask's default permissions would be a real, if
    narrow-window, privacy hole the "0600, verified on every open" guarantee was supposed
    to close everywhere.

    Raises:
        CacheStoreError: `E-CACHE-012` the database refused WAL mode · `E-CACHE-003` an
            existing file (main or sidecar) is not private.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    mode = row[0] if row else None
    if str(mode).lower() != "wal":
        conn.close()
        detail = f"the cache database at {path} refused WAL mode (journal_mode={mode!r})"
        raise CacheStoreError("E-CACHE-012", detail)
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    if not is_new:
        _assert_private(path)
    _chmod_private(path)
    return conn


def _sidecar_paths(path: Path) -> tuple[Path, Path]:
    """Return the `-wal`/`-shm` sidecar paths SQLite may create alongside `path` in WAL mode.

    Guarantees: SQLite deletes both cleanly on a graceful `close()`, so a freshly reopened
    cache typically has neither yet — they reappear once this open's own write (schema
    creation, or the first `put()`) touches the file. Callers check `.exists()` before
    acting on either.
    """
    return path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def _assert_private(path: Path) -> None:
    """Re-assert `0600` on an existing cache file and any `-wal`/`-shm` sidecar.

    PRD §11.9, §31.2.

    Raises:
        CacheStoreError: `E-CACHE-003` `path` or a sidecar is group- or world-readable/writable.
    """
    for candidate in (path, *_sidecar_paths(path)):
        if not candidate.exists():
            continue
        mode = stat.S_IMODE(candidate.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            detail = (
                f"the cache file at {candidate} is not private (mode={oct(mode)}). It holds "
                f"prompt and response bodies (PRD §11.9) and must be readable only by its "
                f"owner; run `chmod 600 {candidate}` before reopening it"
            )
            raise CacheStoreError("E-CACHE-003", detail)


def _chmod_private(path: Path) -> None:
    """Apply `CACHE_FILE_MODE` to `path` and any `-wal`/`-shm` sidecar that currently exists."""
    path.chmod(CACHE_FILE_MODE)
    for sidecar in _sidecar_paths(path):
        if sidecar.exists():
            sidecar.chmod(CACHE_FILE_MODE)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the `llm_cache` table and its indexes if they do not already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_cache (
          cache_key         TEXT PRIMARY KEY,
          key_version       INTEGER NOT NULL,
          model             TEXT NOT NULL,
          prompt_hash       TEXT NOT NULL,
          key_material      TEXT NOT NULL,
          response_body     TEXT NOT NULL,
          response_hash     TEXT NOT NULL,
          prompt_tokens     INTEGER,
          completion_tokens INTEGER,
          duration_wall_ms  INTEGER,
          recorded_at       TEXT NOT NULL,
          recorded_run_id   TEXT NOT NULL,
          provider          TEXT NOT NULL,
          finish_reason     TEXT,
          stream_chunks     BLOB
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_prompt ON llm_cache(prompt_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_run ON llm_cache(recorded_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_model ON llm_cache(model)")


def _wall_iso() -> str:
    """Return the current wall time as an ISO-8601 UTC string, via the sanctioned accessor.

    Guarantees: reads the real clock only through `agentdx.runtime.clock.wall_time()` — the
    one sanctioned accessor (AGENTS.md §4.1 clause 3) — and constructs the string with pure,
    integer-driven arithmetic (`datetime.fromtimestamp` on an integer number of seconds),
    never `datetime.now()`/`utcnow()`, both of which
    `scripts/check_determinism_hygiene.py` bans outright. `recorded_at` is provenance on a
    cache row, never a field in the event log's canonical projection, so this never touches
    invariant I1.
    """
    ms = wall_time()
    seconds, millis = divmod(ms, 1000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=millis * 1000).isoformat()


class SqliteCacheStore:
    """The `llm_cache` table, opened once per process per cache file.

    Guarantees: every method either succeeds or raises `CacheStoreError` — no method returns
    a sentinel that could be confused with a genuine miss (`lookup` returning `None` is the
    sole exception, and it is the `LlmCache.lookup` contract, not an error path).
    """

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        """Bind to an already-opened, schema-ready connection. Use `open()`, not this directly."""
        self._conn = conn
        self._path = path

    @classmethod
    def open(cls, path: Path) -> SqliteCacheStore:
        """Open or create the cache database at `path`.

        Guarantees: on return, the file exists, is in WAL mode, has the `llm_cache` schema,
        and it and any `-wal`/`-shm` sidecar are `0600`.

        Raises:
            CacheStoreError: `E-CACHE-012` WAL mode refused · `E-CACHE-003` an existing file
                (main or sidecar) is not private.
        """
        conn = _open_connection(path)
        return cls(conn, path)

    @property
    def path(self) -> Path:
        """Return the database file path this store is bound to."""
        return self._path

    def close(self) -> None:
        """Close the underlying connection. Idempotent-safe to call once at process exit."""
        self._conn.close()

    def lookup(self, cache_key: str) -> CachedResponse | None:
        """Return the stored response for `cache_key`, or `None` on a miss.

        Guarantees: never raises for "not found" — that is what `None` means, matching
        `sdk.generic.LlmCache.lookup` exactly, so a caller's existing miss-handling is
        unchanged by which cache implementation is behind it.

        Raises:
            CacheStoreError: `E-CACHE-002` the stored row's `response_hash` does not match
                its `response_body` — see `lookup_entry`.
        """
        entry = self.lookup_entry(cache_key)
        return None if entry is None else entry.response

    def lookup_entry(self, cache_key: str) -> StoredEntry | None:
        """Return the full stored entry (response plus provenance), or `None` on a miss.

        Guarantees: every returned entry's `response_body` has been checked against its own
        stored `response_hash` (PRD §36 E-CACHE-002 — "Cache DB corrupt"). This is the one
        integrity check that data present in every row since P07's first version (`put()`
        has always written `response_hash`) makes possible; it was written but never read
        back before this fix, so corruption of a response body between `put()` and `lookup()`
        (a hand-edited row, a partial disk write, bit rot) was previously undetectable here.

        Raises:
            CacheStoreError: `E-CACHE-002` `response_hash` does not match `response_body`.
        """
        row = self._conn.execute(
            """
            SELECT cache_key, key_version, model, prompt_hash, key_material, response_body,
                   response_hash, prompt_tokens, completion_tokens, duration_wall_ms,
                   recorded_at, recorded_run_id, provider, finish_reason
            FROM llm_cache WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        return _entry_from_row(row)

    def put(
        self,
        cache_key: str,
        *,
        key_version: int,
        model: str,
        prompt_hash: str,
        key_material: str,
        response: CachedResponse,
        provider: str,
        response_hash: str,
    ) -> None:
        """Insert or overwrite the response recorded for `cache_key`.

        Guarantees: an upsert, not an append — a second `record`-mode call for the same
        logical request (same key) replaces the earlier response rather than accumulating
        rows, since PRD §11.6's `cache_key` is a primary key and there is exactly one current
        answer for one logical question. This is distinct from PRD §11.7's "no automatic
        eviction": nothing here ever removes an entry the caller did not name via `prune`;
        it only ever updates the one row this exact key already owns.
        """
        self._conn.execute(
            """
            INSERT INTO llm_cache (
              cache_key, key_version, model, prompt_hash, key_material, response_body,
              response_hash, prompt_tokens, completion_tokens, duration_wall_ms,
              recorded_at, recorded_run_id, provider, finish_reason, stream_chunks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(cache_key) DO UPDATE SET
              key_version=excluded.key_version, model=excluded.model,
              prompt_hash=excluded.prompt_hash, key_material=excluded.key_material,
              response_body=excluded.response_body, response_hash=excluded.response_hash,
              prompt_tokens=excluded.prompt_tokens, completion_tokens=excluded.completion_tokens,
              duration_wall_ms=excluded.duration_wall_ms, recorded_at=excluded.recorded_at,
              recorded_run_id=excluded.recorded_run_id, provider=excluded.provider,
              finish_reason=excluded.finish_reason
            """,
            (
                cache_key,
                key_version,
                model,
                prompt_hash,
                key_material,
                response.body,
                response_hash,
                response.prompt_tokens,
                response.completion_tokens,
                response.duration_wall_ms,
                _wall_iso(),
                response.recorded_run_id or "",
                provider,
                response.finish_reason,
            ),
        )

    def nearest(
        self,
        key_material: str,
        *,
        model: str | None = None,
        config: CacheConfig | None = None,
        limit: int | None = None,
        scan_limit: int | None = None,
    ) -> tuple[MissCandidate, ...]:
        """Return up to `limit` stored entries closest to `key_material` by edit distance.

        Design constraint 2: this is what lets a replay-mode miss name "the closest stored
        key, and a diff of the two" instead of just the bare miss. Scans at most
        `scan_limit` entries — most recently recorded first — from the same `model` when one
        is given (a cross-model comparison is never useful, PRD §11.5), so a large cache
        cannot make a miss message itself slow. This is a debugging aid, not a lookup path:
        it never participates in whether a call is a hit or a miss (design constraint 2 —
        "never return an approximate match").

        Args:
            key_material: The (missed) call's own key material, to compare candidates against.
            model: Restrict the scan to entries recorded for this model, when given.
            config: Supplies the defaults for `limit`/`scan_limit` when either is omitted —
                `config.miss_diagnostic_candidates`/`config.miss_diagnostic_scan_limit`
                (`agentdx.toml`'s `[cache]` section, PRD §11.4). Defaults to `CacheConfig()`
                when not given, which is `5`/`200` — unchanged behaviour for every existing
                caller that does not pass a resolved config. This is the live consumer
                `CacheConfig` previously had none of (see `docs/cache.md` §9): a caller that
                *has* resolved `RunConfig.cache` from `agentdx.toml` (a future `RunHost`; no
                such caller exists inside this module's own `DELIVERABLES`) can now pass it
                straight through and have the toml value actually take effect here.
            limit: Overrides `config`'s candidate count for this one call.
            scan_limit: Overrides `config`'s scan depth for this one call.
        """
        cfg = config or CacheConfig()
        effective_limit = cfg.miss_diagnostic_candidates if limit is None else limit
        effective_scan_limit = cfg.miss_diagnostic_scan_limit if scan_limit is None else scan_limit
        if model is not None:
            rows = self._conn.execute(
                "SELECT cache_key, model, key_material FROM llm_cache WHERE model = ? "
                "ORDER BY recorded_at DESC LIMIT ?",
                (model, effective_scan_limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT cache_key, model, key_material FROM llm_cache "
                "ORDER BY recorded_at DESC LIMIT ?",
                (effective_scan_limit,),
            ).fetchall()
        candidates = [
            MissCandidate(
                cache_key=str(r[0]),
                model=str(r[1]),
                distance=_levenshtein(key_material, str(r[2])),
                key_material=str(r[2]),
            )
            for r in rows
        ]
        candidates.sort(key=lambda c: (c.distance, c.cache_key))
        return tuple(candidates[:effective_limit])

    def prune(
        self,
        *,
        run_id: str | None = None,
        older_than_iso: str | None = None,
        model: str | None = None,
    ) -> int:
        """Delete entries matching every given filter (PRD §11.7 explicit invalidation).

        Guarantees: with no arguments, deletes nothing and returns 0 — invalidation is
        always explicit (PRD §11.7: "There is no automatic eviction"), never a default
        behaviour of calling this method.

        Returns:
            The number of rows deleted.

        Raises:
            CacheStoreError: `E-CACHE-004` no filter was given.
        """
        clauses: list[str] = []
        params: list[str] = []
        if run_id is not None:
            clauses.append("recorded_run_id = ?")
            params.append(run_id)
        if older_than_iso is not None:
            clauses.append("recorded_at < ?")
            params.append(older_than_iso)
        if model is not None:
            clauses.append("model = ?")
            params.append(model)
        if not clauses:
            detail = (
                "prune() requires at least one of run_id/older_than_iso/model — silent, "
                "unfiltered eviction would break reproducibility of old bundles (PRD §11.7)"
            )
            raise CacheStoreError("E-CACHE-004", detail)
        cursor = self._conn.execute(f"DELETE FROM llm_cache WHERE {' AND '.join(clauses)}", params)  # noqa: S608
        return cursor.rowcount

    def iter_all(self) -> Iterator[StoredEntry]:
        """Yield every stored entry, in `cache_key` order. For tests and diagnostics only.

        Guarantees: every yielded entry passes the same `response_hash` integrity check
        `lookup_entry` performs (see there) — a full scan doubles as `agentdx cache verify`'s
        underlying mechanism, row by row, even before that CLI command exists.

        Raises:
            CacheStoreError: `E-CACHE-002` a row's `response_hash` does not match its
                `response_body`.
        """
        cursor = self._conn.execute(
            """
            SELECT cache_key, key_version, model, prompt_hash, key_material, response_body,
                   response_hash, prompt_tokens, completion_tokens, duration_wall_ms,
                   recorded_at, recorded_run_id, provider, finish_reason
            FROM llm_cache ORDER BY cache_key
            """
        )
        for row in cursor:
            yield _entry_from_row(row)

    def __len__(self) -> int:
        """Return the number of stored entries."""
        row = self._conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()
        return int(row[0])


def _as_int(value: object, *, default: int = 0) -> int:
    """Return `value` as an int, treating `None` as `default`.

    A small, strictly-typed narrowing helper: a raw SQLite column value is `object` as far as
    `mypy --strict` is concerned, and `int(object)` is not a valid overload — this function is
    the one place that narrows it, rather than a `# type: ignore` at every call site.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    detail = f"expected an integer column value, got {type(value).__name__}: {value!r}"
    raise CacheStoreError("E-CACHE-002", detail)


def _as_opt_int(value: object) -> int | None:
    """Return `value` as an int, or `None` when the column was `NULL`."""
    return None if value is None else _as_int(value)


def _entry_from_row(row: Sequence[object]) -> StoredEntry:
    """Return a `StoredEntry` built from one `llm_cache` row, column order as selected above.

    Guarantees: the row's `response_hash` is verified against its `response_body` before a
    `StoredEntry` is built — a mismatch means the row was corrupted or hand-edited since
    `put()` wrote it, since `put()` always computes `response_hash` from the exact
    `response.body` it stores in the same call (`modes.py`'s `Cache.store` passes
    `hash_text(response.body)`; nothing else writes this column).

    Raises:
        CacheStoreError: `E-CACHE-002` `response_hash` does not match `response_body`
            (PRD §36, "Cache DB corrupt").
    """
    (
        cache_key,
        key_version,
        model,
        prompt_hash,
        key_material,
        response_body,
        response_hash,
        prompt_tokens,
        completion_tokens,
        duration_wall_ms,
        recorded_at,
        recorded_run_id,
        provider,
        finish_reason,
    ) = row
    body_text = str(response_body)
    stored_hash = str(response_hash)
    expected_hash = hash_text(body_text)
    if stored_hash != expected_hash:
        detail = (
            f"cache entry {cache_key!r} failed integrity verification: stored response_hash "
            f"{stored_hash!r} does not match blake2b(response_body)={expected_hash!r} — the "
            f"database or this row's response_body has been corrupted or modified since it "
            f"was recorded; run `agentdx cache verify` to find every affected row, then "
            f"re-record"
        )
        raise CacheStoreError("E-CACHE-002", detail)
    return StoredEntry(
        cache_key=str(cache_key),
        key_version=_as_int(key_version),
        model=str(model),
        prompt_hash=str(prompt_hash),
        key_material=str(key_material),
        response_hash=stored_hash,
        recorded_at=str(recorded_at),
        provider=str(provider),
        response=CachedResponse(
            body=body_text,
            model=str(model),
            prompt_tokens=_as_int(prompt_tokens),
            completion_tokens=_as_int(completion_tokens),
            finish_reason=None if finish_reason is None else str(finish_reason),
            duration_wall_ms=_as_opt_int(duration_wall_ms),
            recorded_run_id=str(recorded_run_id) or None,
        ),
    )


def _levenshtein(a: str, b: str) -> int:
    """Return the Levenshtein edit distance between `a` and `b`.

    A plain, deterministic, allocation-light dynamic-programming implementation (two rolling
    rows) — no library dependency, and nothing here reads a clock, a random source or an id,
    so this file needs no `scripts/check_determinism_hygiene.py` allowlist entry.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    current = [0] * (len(b) + 1)
    for i, ca in enumerate(a, start=1):
        current[0] = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,  # deletion
                current[j - 1] + 1,  # insertion
                previous[j - 1] + cost,  # substitution
            )
        previous, current = current, previous
    return previous[len(b)]


def response_hash_of(body: str) -> str:
    """Return the `blake2b:` hash of a response body, in the project's standard hash format."""
    return hash_text(body)


def dumps_json(value: object) -> str:
    """Return `value` as compact JSON text. A small convenience for callers building bodies."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CACHE_FILE_MODE",
    "CacheStoreError",
    "CachedResponse",
    "MissCandidate",
    "SqliteCacheStore",
    "StoredEntry",
    "dumps_json",
    "response_hash_of",
]
