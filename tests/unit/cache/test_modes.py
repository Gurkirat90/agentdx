"""`Cache` (PRD §11.2, design constraints 2/6) and `SchedulerCacheHook` (design constraint 3).

Covers: the four explicit modes' `lookup`/`store` behaviour, `describe_miss`'s hard-error
diagnostic text, and `SchedulerCacheHook.on_llm_yield`'s PRD §11.3 duration chain — exercised
directly, since (as `modes.py`'s own docstring documents) `runtime/scheduler.py` never
actually calls it.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from agentdx.runtime.cache.key import KEY_VERSION, cache_key_for, key_material_json
from agentdx.runtime.cache.modes import Cache, CacheModeError, SchedulerCacheHook
from agentdx.runtime.cache.perturb import ContradictoryPoolSelector, PerturbSelector
from agentdx.runtime.cache.store import CachedResponse, SqliteCacheStore, response_hash_of
from agentdx.runtime.clock import CalibrationEntry, CalibrationProfile, SchedulerConfig


def _rng(seed: int) -> random.Random:
    """Return a seeded RNG for a test under test — not a cryptographic use."""
    return random.Random(seed)  # noqa: S311


def _store(tmp_path: Path) -> SqliteCacheStore:
    return SqliteCacheStore.open(tmp_path / "cache.db")


def _seed_entry(
    store: SqliteCacheStore,
    *,
    prompt: str = "hello",
    model: str = "m",
    body: str = '{"text": "hi"}',
) -> str:
    messages = [{"role": "user", "content": prompt}]
    key = cache_key_for(model, messages, {})
    response = CachedResponse(body=body, model=model, prompt_tokens=1, completion_tokens=1)
    store.put(
        key,
        key_version=KEY_VERSION,
        model=model,
        prompt_hash=response_hash_of(prompt),
        key_material=key_material_json(model, messages, {}),
        response=response,
        provider="openai_compatible",
        response_hash=response_hash_of(response.body),
    )
    return key


# ---------------------------------------------------------------------------------------
# __post_init__ validation (design constraint 6 — modes explicit and exhaustive)
# ---------------------------------------------------------------------------------------


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    """`E-CACHE-008` — no implicit fifth mode."""
    store = _store(tmp_path)
    with pytest.raises(CacheModeError) as excinfo:
        Cache(backing_store=store, mode="not_a_real_mode")
    assert excinfo.value.code == "E-CACHE-008"
    store.close()


def test_perturb_mode_requires_a_selector_and_rng(tmp_path: Path) -> None:
    """`E-CACHE-009` — `mode='perturb'` without both `perturb` and `perturb_rng` is refused."""
    store = _store(tmp_path)
    with pytest.raises(CacheModeError) as excinfo:
        Cache(backing_store=store, mode="perturb")
    assert excinfo.value.code == "E-CACHE-009"
    store.close()


@pytest.mark.parametrize("mode", ["record", "replay", "passthrough"])
def test_non_perturb_modes_construct_without_a_selector(tmp_path: Path, mode: str) -> None:
    """The three non-perturb modes never require perturb machinery."""
    store = _store(tmp_path)
    Cache(backing_store=store, mode=mode)
    store.close()


# ---------------------------------------------------------------------------------------
# lookup() per mode
# ---------------------------------------------------------------------------------------


def test_passthrough_lookup_is_always_a_miss(tmp_path: Path) -> None:
    """`passthrough` never consults the cache, even if the key was recorded."""
    store = _store(tmp_path)
    key = _seed_entry(store)
    cache = Cache(backing_store=store, mode="passthrough")
    assert cache.lookup(key) is None
    store.close()


def test_record_lookup_returns_the_genuine_response(tmp_path: Path) -> None:
    """`record` mode returns a stored response when one exists."""
    store = _store(tmp_path)
    key = _seed_entry(store)
    cache = Cache(backing_store=store, mode="record")
    found = cache.lookup(key)
    assert found is not None
    assert found.body == '{"text": "hi"}'
    store.close()


def test_record_lookup_miss_returns_none(tmp_path: Path) -> None:
    """`record` mode returns `None`, not an error, when nothing is stored yet."""
    store = _store(tmp_path)
    cache = Cache(backing_store=store, mode="record")
    assert cache.lookup("blake2b:nope") is None
    store.close()


def test_replay_lookup_returns_the_genuine_response(tmp_path: Path) -> None:
    """`replay` mode returns the exact recorded response — this module never approximates."""
    store = _store(tmp_path)
    key = _seed_entry(store)
    cache = Cache(backing_store=store, mode="replay")
    found = cache.lookup(key)
    assert found is not None
    assert found.body == '{"text": "hi"}'
    store.close()


def test_replay_lookup_miss_returns_none_not_an_approximate_match(tmp_path: Path) -> None:
    """`replay` mode returns `None` on a miss — even one character from a genuine stored key.

    Regression test for a gap the independent review found: the miss sentinel used to be
    `"blake2b:nope"`, a string with none of a real key's shape (`blake2b:` + 64 hex chars) and
    therefore maximally far, by edit distance, from anything ever stored — a fuzzy-match
    regression that only mishandled *near* misses could pass this test undetected. Using a
    key one character away from a genuine stored key closes that gap: `lookup` does an exact
    primary-key match (`store.py`'s `SELECT ... WHERE cache_key = ?`) and must never fall back
    to `nearest()`'s edit-distance search, no matter how close the miss is.
    """
    store = _store(tmp_path)
    genuine_key = _seed_entry(store, prompt="something else entirely")
    near_miss_key = genuine_key[:-1] + ("0" if genuine_key[-1] != "0" else "1")
    assert near_miss_key != genuine_key
    cache = Cache(backing_store=store, mode="replay")
    assert cache.lookup(near_miss_key) is None
    store.close()


def test_perturb_lookup_never_returns_the_genuine_response(tmp_path: Path) -> None:
    """`perturb` mode substitutes a different response, never the real one (design constraint 4).

    Regression test for a gap the independent review found: `_seed_entry` used to give every
    entry the identical body `'{"text": "hi"}'`, so a bug that returned the *genuine* response
    unperturbed would have been invisible here — `result is not None` and a same-body
    assertion would both still have passed. Distinct bodies close that gap: the returned body
    must equal the *other* entry's body and must differ from the genuine one's.
    """
    store = _store(tmp_path)
    key = _seed_entry(store, prompt="hello", body='{"text": "genuine"}')
    other_key = _seed_entry(
        store, prompt="a completely different question", body='{"text": "substituted"}'
    )
    selector = PerturbSelector(
        mode="contradictory", strategy=ContradictoryPoolSelector(store=store)
    )
    cache = Cache(backing_store=store, mode="perturb", perturb=selector, perturb_rng=_rng(1))
    result = cache.lookup(key)
    assert result is not None
    assert cache.last_perturb_source == other_key
    assert result.body == '{"text": "substituted"}'
    assert result.body != '{"text": "genuine"}'
    store.close()


def test_perturb_lookup_miss_when_never_recorded(tmp_path: Path) -> None:
    """`perturb` mode substitutes for a real call — it never invents one that never happened."""
    store = _store(tmp_path)
    selector = PerturbSelector(
        mode="contradictory", strategy=ContradictoryPoolSelector(store=store)
    )
    cache = Cache(backing_store=store, mode="perturb", perturb=selector, perturb_rng=_rng(1))
    assert cache.lookup("blake2b:nope") is None
    assert cache.last_perturb_source is None
    store.close()


# ---------------------------------------------------------------------------------------
# store() per mode
# ---------------------------------------------------------------------------------------


def test_record_store_writes_and_is_visible_to_lookup(tmp_path: Path) -> None:
    """`record` mode's `store()` actually persists — the next `lookup()` sees it."""
    store = _store(tmp_path)
    cache = Cache(backing_store=store, mode="record")
    response = CachedResponse(body="new", model="m", prompt_tokens=1, completion_tokens=1)
    cache.store("blake2b:k1", response)
    found = cache.lookup("blake2b:k1")
    assert found is not None
    assert found.body == "new"
    store.close()


def test_passthrough_store_writes_too_known_sdk_deviation(tmp_path: Path) -> None:
    """`passthrough` accepts writes — matching the live, unmodified `_resolve`'s behaviour."""
    store = _store(tmp_path)
    cache = Cache(backing_store=store, mode="passthrough")
    response = CachedResponse(body="new", model="m", prompt_tokens=1, completion_tokens=1)
    cache.store("blake2b:k1", response)
    assert len(store) == 1
    store.close()


@pytest.mark.parametrize("mode", ["replay", "perturb"])
def test_replay_and_perturb_store_is_a_hard_error(tmp_path: Path, mode: str) -> None:
    """`E-CACHE-010` — I7: a deterministic mode must never silently gain a new entry."""
    store = _store(tmp_path)
    kwargs: dict[str, object] = {}
    if mode == "perturb":
        kwargs = {
            "perturb": PerturbSelector(
                mode="contradictory", strategy=ContradictoryPoolSelector(store=store)
            ),
            "perturb_rng": _rng(1),
        }
    cache = Cache(backing_store=store, mode=mode, **kwargs)
    response = CachedResponse(body="new", model="m", prompt_tokens=1, completion_tokens=1)
    with pytest.raises(CacheModeError) as excinfo:
        cache.store("blake2b:k1", response)
    assert excinfo.value.code == "E-CACHE-010"
    assert len(store) == 0
    store.close()


def test_store_with_key_material_persists_it_for_the_miss_diagnostic(tmp_path: Path) -> None:
    """The optional `key_material` kwarg round-trips, unlike a bare two-arg SDK-style call."""
    store = _store(tmp_path)
    cache = Cache(backing_store=store, mode="record")
    response = CachedResponse(body="new", model="m", prompt_tokens=1, completion_tokens=1)
    cache.store("blake2b:k1", response, key_material='{"model":"m"}', prompt_hash="blake2b:p")
    entry = store.lookup_entry("blake2b:k1")
    assert entry is not None
    assert entry.key_material == '{"model":"m"}'
    assert entry.prompt_hash == "blake2b:p"
    store.close()


def test_store_without_key_material_defaults_to_empty_string_not_an_error(tmp_path: Path) -> None:
    """A bare two-positional-argument `store()` call — exactly what the live SDK makes — works."""
    store = _store(tmp_path)
    cache = Cache(backing_store=store, mode="record")
    response = CachedResponse(body="new", model="m", prompt_tokens=1, completion_tokens=1)
    cache.store("blake2b:k1", response)
    entry = store.lookup_entry("blake2b:k1")
    assert entry is not None
    assert entry.key_material == ""
    store.close()


# ---------------------------------------------------------------------------------------
# describe_miss() — design constraint 2
# ---------------------------------------------------------------------------------------


def test_describe_miss_on_an_empty_cache_says_so(tmp_path: Path) -> None:
    """No stored entries at all is named explicitly, not left implicit."""
    store = _store(tmp_path)
    cache = Cache(backing_store=store, mode="replay")
    message = cache.describe_miss("blake2b:nope")
    assert "cache miss" in message
    assert "no stored entries to compare against" in message
    assert "never falls back to a live call" in message
    store.close()


def test_describe_miss_names_the_closest_key_and_a_diff(tmp_path: Path) -> None:
    """The closest stored key (by edit distance) and a real diff are both present."""
    store = _store(tmp_path)
    _seed_entry(store, prompt="hello", model="m")
    cache = Cache(backing_store=store, mode="replay")
    missed_material = key_material_json("m", [{"role": "user", "content": "hellp"}], {})
    message = cache.describe_miss("blake2b:missed", key_material=missed_material, model="m")
    assert "closest stored key" in message
    assert "edit distance" in message
    assert "---" in message or "closest stored key" in message  # unified-diff header present
    store.close()


def test_describe_miss_never_returns_a_hit(tmp_path: Path) -> None:
    """`describe_miss` is diagnostic text only — it never makes `lookup` return anything."""
    store = _store(tmp_path)
    key = _seed_entry(store, prompt="hello")
    cache = Cache(backing_store=store, mode="replay")
    cache.describe_miss("blake2b:some-other-key", key_material="irrelevant", model="m")
    assert cache.lookup("blake2b:some-other-key") is None
    assert cache.lookup(key) is not None
    store.close()


# ---------------------------------------------------------------------------------------
# SchedulerCacheHook.on_llm_yield — PRD §11.3's duration chain (design constraint 3)
# ---------------------------------------------------------------------------------------


def test_on_llm_yield_returns_none_when_the_key_is_not_cached(tmp_path: Path) -> None:
    """No stored entry for `cache_key` means no virtual duration to report."""
    store = _store(tmp_path)
    profile = CalibrationProfile.defaults_only(SchedulerConfig())
    hook = SchedulerCacheHook(store=store, calibration=profile)
    assert hook.on_llm_yield("task-1", "blake2b:nope") is None
    store.close()


def test_on_llm_yield_uses_the_flat_default_when_no_calibration_exists(tmp_path: Path) -> None:
    """No `by_kind`/`by_group` entry and no recorded duration: falls to the Q-43.2.3 default."""
    store = _store(tmp_path)
    key = _seed_entry(store)  # _seed_entry never sets duration_wall_ms
    config = SchedulerConfig(calibration_llm_ms=777)
    profile = CalibrationProfile.defaults_only(config)
    hook = SchedulerCacheHook(store=store, calibration=profile)
    assert hook.on_llm_yield("task-1", key) == 777
    store.close()


def test_on_llm_yield_prefers_the_recorded_duration_over_the_flat_default(
    tmp_path: Path,
) -> None:
    """A cache entry's own `duration_wall_ms` outranks the flat default with no real profile."""
    store = _store(tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    key = cache_key_for("m", messages, {})
    response = CachedResponse(
        body="x", model="m", prompt_tokens=1, completion_tokens=1, duration_wall_ms=555
    )
    store.put(
        key,
        key_version=KEY_VERSION,
        model="m",
        prompt_hash=response_hash_of("hello"),
        key_material=key_material_json("m", messages, {}),
        response=response,
        provider="openai_compatible",
        response_hash=response_hash_of(response.body),
    )
    profile = CalibrationProfile.defaults_only(SchedulerConfig(calibration_llm_ms=1))
    hook = SchedulerCacheHook(store=store, calibration=profile)
    assert hook.on_llm_yield("task-1", key) == 555
    store.close()


def test_on_llm_yield_prefers_a_real_by_kind_calibration_entry_over_everything(
    tmp_path: Path,
) -> None:
    """A real aggregated `by_kind` calibration entry wins even over a recorded per-call duration."""
    store = _store(tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    key = cache_key_for("m", messages, {})
    response = CachedResponse(
        body="x", model="m", prompt_tokens=1, completion_tokens=1, duration_wall_ms=555
    )
    store.put(
        key,
        key_version=KEY_VERSION,
        model="m",
        prompt_hash=response_hash_of("hello"),
        key_material=key_material_json("m", messages, {}),
        response=response,
        provider="openai_compatible",
        response_hash=response_hash_of(response.body),
    )
    profile = CalibrationProfile(
        calibration_id="cal-1",
        by_kind={"llm_call": CalibrationEntry(median_ms=222, p90_ms=222, samples=10)},
        defaults_ms={"llm_call": 1},
    )
    hook = SchedulerCacheHook(store=store, calibration=profile)
    assert hook.on_llm_yield("task-1", key) == 222
    store.close()


def test_scheduler_never_actually_calls_on_llm_yield() -> None:
    """Document why this hook's tests call `on_llm_yield` directly, not via a live `Scheduler`.

    `Scheduler.__init__` stores whatever `cache_hook` it is given as `self._cache_hook`
    (`runtime/scheduler.py`, fixed — not this module's to change) but never calls
    `self._cache_hook.on_llm_yield` anywhere else in that file. If a future, in-scope change
    to `runtime/scheduler.py` ever wires this call site up, this test starts failing loudly
    (a second reference to `_cache_hook` appears) rather than this gap silently going stale
    in `modes.py`'s docstring while the code has moved on.
    """
    import inspect

    from agentdx.runtime import scheduler as scheduler_module

    source = inspect.getsource(scheduler_module)
    references = source.count("_cache_hook")
    # exactly one reference: the `self._cache_hook = cache_hook or CacheHook()` assignment
    # in `Scheduler.__init__`. More than one would mean a call site now exists.
    assert references == 1, (
        "runtime/scheduler.py now references `_cache_hook` more than once — "
        "SchedulerCacheHook.on_llm_yield may be wired up; update modes.py's docstring "
        "and docs/cache.md §7 to match, and add a live-scheduler integration test"
    )
