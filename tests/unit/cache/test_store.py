"""`SqliteCacheStore` (PRD §11.6, §11.7, §11.9, §31.2) and design constraint 2's diagnostic.

Covers: round-trip storage, `0600` enforcement on create and on every reopen, `prune()`'s
"at least one filter" invariant (PRD §11.7 — no silent unfiltered eviction), and `nearest()`'s
edit-distance ordering (the data `Cache.describe_miss` renders into a diff).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import stat
from pathlib import Path

import pytest

from agentdx.config import CacheConfig
from agentdx.runtime.cache.key import KEY_VERSION, cache_key_for, key_material_json
from agentdx.runtime.cache.store import (
    CACHE_FILE_MODE,
    CachedResponse,
    CacheStoreError,
    SqliteCacheStore,
    response_hash_of,
)
from agentdx.sdk.generic import CachedResponse as SdkCachedResponse

MESSAGES = [{"role": "user", "content": "hello"}]


def _store(tmp_path: Path) -> SqliteCacheStore:
    return SqliteCacheStore.open(tmp_path / "cache.db")


def _put(
    store: SqliteCacheStore, *, model: str = "m", prompt: str = "hello", run_id: str = "run-1"
) -> str:
    messages = [{"role": "user", "content": prompt}]
    key = cache_key_for(model, messages, {})
    material = key_material_json(model, messages, {})
    response = CachedResponse(
        body='{"text": "hi"}',
        model=model,
        prompt_tokens=3,
        completion_tokens=2,
        finish_reason="stop",
        duration_wall_ms=42,
        recorded_run_id=run_id,
    )
    store.put(
        key,
        key_version=KEY_VERSION,
        model=model,
        prompt_hash=response_hash_of(prompt),
        key_material=material,
        response=response,
        provider="openai_compatible",
        response_hash=response_hash_of(response.body),
    )
    return key


# ---------------------------------------------------------------------------------------
# CachedResponse field compatibility (store.py's own claim, checked rather than assumed)
# ---------------------------------------------------------------------------------------


def test_cached_response_field_set_matches_the_sdk_dataclass() -> None:
    """`store.CachedResponse` and `sdk.generic.CachedResponse` must stay field-compatible."""
    ours = {f.name for f in dataclasses.fields(CachedResponse)}
    theirs = {f.name for f in dataclasses.fields(SdkCachedResponse)}
    assert ours == theirs


# ---------------------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------------------


def test_put_then_lookup_round_trips(tmp_path: Path) -> None:
    """A recorded response is returned verbatim by `lookup`."""
    store = _store(tmp_path)
    key = _put(store)
    found = store.lookup(key)
    assert found is not None
    assert found.body == '{"text": "hi"}'
    assert found.prompt_tokens == 3
    assert found.completion_tokens == 2
    assert found.finish_reason == "stop"
    assert found.duration_wall_ms == 42
    assert found.recorded_run_id == "run-1"
    store.close()


def test_lookup_miss_returns_none_not_an_error(tmp_path: Path) -> None:
    """A miss is `None`, matching `sdk.generic.LlmCache.lookup` — never an exception."""
    store = _store(tmp_path)
    assert store.lookup("blake2b:doesnotexist") is None
    store.close()


def test_put_is_an_upsert_not_an_append(tmp_path: Path) -> None:
    """A second `put` for the same key replaces the row rather than accumulating one."""
    store = _store(tmp_path)
    key = _put(store, prompt="hello")
    messages = [{"role": "user", "content": "hello"}]
    updated = CachedResponse(
        body='{"text": "updated"}',
        model="m",
        prompt_tokens=9,
        completion_tokens=9,
        recorded_run_id="run-2",
    )
    store.put(
        key,
        key_version=KEY_VERSION,
        model="m",
        prompt_hash=response_hash_of("hello"),
        key_material=key_material_json("m", messages, {}),
        response=updated,
        provider="openai_compatible",
        response_hash=response_hash_of(updated.body),
    )
    assert len(store) == 1
    found = store.lookup(key)
    assert found is not None
    assert found.body == '{"text": "updated"}'
    store.close()


def test_lookup_entry_exposes_key_material_for_diagnostics(tmp_path: Path) -> None:
    """`lookup_entry` returns the stored `key_material`, the text a diff is built from."""
    store = _store(tmp_path)
    key = _put(store)
    entry = store.lookup_entry(key)
    assert entry is not None
    assert entry.cache_key == key
    assert entry.key_version == KEY_VERSION
    assert '"hello"' in entry.key_material
    store.close()


def test_iter_all_and_len(tmp_path: Path) -> None:
    """`iter_all` yields every row; `len()` matches the row count."""
    store = _store(tmp_path)
    _put(store, model="a", prompt="one")
    _put(store, model="b", prompt="two")
    assert len(store) == 2
    assert {e.model for e in store.iter_all()} == {"a", "b"}
    store.close()


# ---------------------------------------------------------------------------------------
# Privacy — 0600 on create, verified (not merely requested) on reopen (PRD §11.9, §31.2)
# ---------------------------------------------------------------------------------------


def test_new_database_is_created_private(tmp_path: Path) -> None:
    """A freshly created cache file is `0600`."""
    store = _store(tmp_path)
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == CACHE_FILE_MODE
    store.close()


def test_reopen_of_a_private_file_succeeds(tmp_path: Path) -> None:
    """Reopening a file that is still `0600` works normally."""
    path = tmp_path / "cache.db"
    SqliteCacheStore.open(path).close()
    store = SqliteCacheStore.open(path)
    assert len(store) == 0
    store.close()


def test_reopen_of_a_world_readable_file_is_a_hard_error(tmp_path: Path) -> None:
    """A cache file someone loosened to group/world access refuses to reopen (`E-CACHE-003`)."""
    path = tmp_path / "cache.db"
    SqliteCacheStore.open(path).close()
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
    with pytest.raises(CacheStoreError) as excinfo:
        SqliteCacheStore.open(path)
    assert excinfo.value.code == "E-CACHE-003"


def test_wal_and_shm_sidecars_are_private_while_the_store_is_open(tmp_path: Path) -> None:
    """The `-wal`/`-shm` sidecars are `0600`, not the process umask's default.

    Checked while the store is still open, deliberately: SQLite deletes both sidecars on a
    clean `close()`, so asserting after close would prove nothing about the exposure window
    that actually matters — while a transaction (here, `put()`'s own write) is in flight and
    the sidecars hold the same prompt/response bodies as the main file (PRD §11.9).
    """
    path = tmp_path / "cache.db"
    store = SqliteCacheStore.open(path)
    _put(store)
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    assert wal.exists(), "expected put() to have created the WAL sidecar by now"
    assert stat.S_IMODE(wal.stat().st_mode) == CACHE_FILE_MODE
    if shm.exists():
        assert stat.S_IMODE(shm.stat().st_mode) == CACHE_FILE_MODE
    store.close()


def test_reopening_a_cache_still_chmods_sidecars_recreated_by_this_open(tmp_path: Path) -> None:
    """A reopen re-privatises sidecars its own schema-creation write brings back."""
    path = tmp_path / "cache.db"
    SqliteCacheStore.open(path).close()
    store = SqliteCacheStore.open(path)
    _put(store)
    wal = path.with_name(path.name + "-wal")
    assert wal.exists()
    assert stat.S_IMODE(wal.stat().st_mode) == CACHE_FILE_MODE
    store.close()


# ---------------------------------------------------------------------------------------
# prune() — explicit invalidation only (PRD §11.7)
# ---------------------------------------------------------------------------------------


def test_prune_with_no_filter_is_a_hard_error(tmp_path: Path) -> None:
    """Calling `prune()` with zero filters raises rather than silently deleting everything."""
    store = _store(tmp_path)
    _put(store)
    with pytest.raises(CacheStoreError) as excinfo:
        store.prune()
    assert excinfo.value.code == "E-CACHE-004"
    assert len(store) == 1
    store.close()


def test_prune_by_run_id_deletes_only_matching_rows(tmp_path: Path) -> None:
    """`prune(run_id=...)` removes only the rows recorded under that run."""
    store = _store(tmp_path)
    _put(store, prompt="a", run_id="keep")
    _put(store, prompt="b", run_id="drop")
    deleted = store.prune(run_id="drop")
    assert deleted == 1
    assert len(store) == 1
    remaining = next(iter(store.iter_all()))
    assert remaining.response.recorded_run_id == "keep"
    store.close()


def test_prune_by_model_deletes_only_matching_rows(tmp_path: Path) -> None:
    """`prune(model=...)` removes only rows for that model."""
    store = _store(tmp_path)
    _put(store, model="keep-model", prompt="a")
    _put(store, model="drop-model", prompt="b")
    deleted = store.prune(model="drop-model")
    assert deleted == 1
    assert len(store) == 1
    store.close()


# ---------------------------------------------------------------------------------------
# nearest() — the "closest stored key" diagnostic (design constraint 2)
# ---------------------------------------------------------------------------------------


def test_nearest_orders_by_edit_distance_ascending(tmp_path: Path) -> None:
    """The closest `key_material` by edit distance sorts first."""
    store = _store(tmp_path)
    _put(store, model="m", prompt="hello world this is a long prompt")
    _put(store, model="m", prompt="hello")
    missed_material = key_material_json("m", [{"role": "user", "content": "hellp"}], {})
    candidates = store.nearest(missed_material, model="m")
    assert candidates
    # the "hello" entry's key_material differs by one character from "hellp"'s material,
    # so it must be strictly closer than the long, unrelated prompt.
    distances = [c.distance for c in candidates]
    assert distances == sorted(distances)
    closest_material = candidates[0].key_material
    assert '"hello"' in closest_material
    store.close()


def test_nearest_restricts_to_the_given_model(tmp_path: Path) -> None:
    """`nearest(..., model=...)` never proposes a candidate from a different model."""
    store = _store(tmp_path)
    _put(store, model="model-a", prompt="hello")
    key = _put(store, model="model-b", prompt="hello")
    missed = store.lookup_entry(key)
    assert missed is not None
    candidates = store.nearest(missed.key_material, model="model-b")
    assert all(c.model == "model-b" for c in candidates)
    store.close()


def test_nearest_returns_empty_on_an_empty_store(tmp_path: Path) -> None:
    """No stored entries means no candidates — not an error."""
    store = _store(tmp_path)
    assert store.nearest("anything") == ()
    store.close()


def test_nearest_defaults_come_from_cache_config(tmp_path: Path) -> None:
    """A passed `CacheConfig` actually changes `nearest()`'s candidate count.

    Regression test for the finding that `CacheConfig` had no live consumer anywhere in the
    codebase: `store.py`'s `nearest()` used to hardcode `limit=5` independently of
    `CacheConfig.miss_diagnostic_candidates`, so changing the config could never have changed
    this method's behaviour even if a caller had wired `agentdx.toml`'s `[cache]` section all
    the way through.
    """
    store = _store(tmp_path)
    for i in range(5):
        _put(store, model="m", prompt=f"hello {i}")
    default_candidates = store.nearest("anything", model="m")
    assert len(default_candidates) == 5  # CacheConfig()'s default miss_diagnostic_candidates
    narrowed = store.nearest(
        "anything", model="m", config=CacheConfig(miss_diagnostic_candidates=2)
    )
    assert len(narrowed) == 2
    store.close()


# ---------------------------------------------------------------------------------------
# Integrity — response_hash verified on every read (PRD §36 E-CACHE-002, "Cache DB corrupt")
# ---------------------------------------------------------------------------------------


def test_lookup_entry_exposes_the_verified_response_hash(tmp_path: Path) -> None:
    """A genuine, unmodified row's `response_hash` is exposed once verified."""
    store = _store(tmp_path)
    key = _put(store)
    entry = store.lookup_entry(key)
    assert entry is not None
    assert entry.response_hash == response_hash_of(entry.response.body)
    store.close()


def test_a_hand_edited_response_body_fails_integrity_verification(tmp_path: Path) -> None:
    """`response_body` changed out from under `response_hash` raises `E-CACHE-002` on lookup.

    Simulates the exact failure mode the review found undetectable: `response_hash` was
    written on every `put()` but never read back anywhere, so this kind of corruption
    (a hand edit, a partial disk write, bit rot) was previously silent — a caller would just
    get back whatever bytes were on disk, hash mismatch or not.
    """
    path = tmp_path / "cache.db"
    store = SqliteCacheStore.open(path)
    key = _put(store)
    store.close()
    conn = sqlite3.connect(str(path))
    conn.execute(
        "UPDATE llm_cache SET response_body = ? WHERE cache_key = ?",
        ('{"text": "tampered"}', key),
    )
    conn.commit()
    conn.close()
    store = SqliteCacheStore.open(path)
    with pytest.raises(CacheStoreError) as excinfo:
        store.lookup(key)
    assert excinfo.value.code == "E-CACHE-002"
    store.close()


def test_iter_all_also_verifies_integrity(tmp_path: Path) -> None:
    """A full scan (`iter_all`) catches corruption in a row `lookup` was never asked about."""
    path = tmp_path / "cache.db"
    store = SqliteCacheStore.open(path)
    _put(store, prompt="untouched")
    tampered_key = _put(store, prompt="tampered-target")
    store.close()
    conn = sqlite3.connect(str(path))
    conn.execute(
        "UPDATE llm_cache SET response_body = ? WHERE cache_key = ?",
        ('{"text": "changed"}', tampered_key),
    )
    conn.commit()
    conn.close()
    store = SqliteCacheStore.open(path)
    with pytest.raises(CacheStoreError) as excinfo:
        list(store.iter_all())
    assert excinfo.value.code == "E-CACHE-002"
    store.close()


class _FakeCursor:
    """A minimal stand-in for `sqlite3.Cursor`, returning one fixed row from `fetchone()`."""

    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _WalRefusingConnection:
    """A fake connection whose `PRAGMA journal_mode=WAL` reports `delete`, never `wal`."""

    def execute(self, sql: str, *_args: object) -> _FakeCursor:
        if "journal_mode" in sql:
            return _FakeCursor(("delete",))
        return _FakeCursor(None)

    def close(self) -> None:
        pass


def test_wal_mode_refused_is_a_distinct_code_from_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WAL-mode-refused (`E-CACHE-012`) is not conflated with data corruption (`E-CACHE-002`).

    Regression test for the review finding that these were the same code: an environment
    that cannot honour WAL mode (e.g. some network filesystems) says nothing about whether
    any *data* in the file is wrong, so it must not raise the "Cache DB corrupt" code PRD §36
    reserves for a real integrity failure. A real WAL refusal is hard to force on a normal
    local filesystem, so this fakes the connection rather than the filesystem.
    """
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: _WalRefusingConnection())
    path = tmp_path / "cache.db"
    with pytest.raises(CacheStoreError) as excinfo:
        SqliteCacheStore.open(path)
    assert excinfo.value.code == "E-CACHE-012"
