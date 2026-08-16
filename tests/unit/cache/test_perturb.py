"""Perturb-mode selection (PRD §11.8, design constraint 4, definition of done 3).

`scripts/check_determinism_hygiene.py` only scans `src/agentdx/`, so this test file is free
to construct its own `random.Random(seed)` — the one thing `src/agentdx/runtime/cache/perturb.py`
itself is forbidden from doing (it only ever receives an already-built `RandomSource`).
"""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path

import pytest

from agentdx.runtime.cache.key import KEY_VERSION, cache_key_for, key_material_json
from agentdx.runtime.cache.perturb import (
    ConfidentWrongPool,
    ContradictoryPoolSelector,
    PerturbError,
    PerturbSelector,
    RunHistory,
    StaleOutputSelector,
)
from agentdx.runtime.cache.store import CachedResponse, SqliteCacheStore, response_hash_of


def _rng(seed: int) -> random.Random:
    """Return a seeded RNG for a test under test — not a cryptographic use."""
    return random.Random(seed)  # noqa: S311


def _response(text: str) -> CachedResponse:
    return CachedResponse(body=text, model="m", prompt_tokens=1, completion_tokens=1)


# ---------------------------------------------------------------------------------------
# stale_output
# ---------------------------------------------------------------------------------------


def test_stale_output_raises_when_no_earlier_call_exists() -> None:
    """`E-CACHE-005` — nothing to draw from yet, in the first call of a run."""
    selector = StaleOutputSelector(history=RunHistory())
    with pytest.raises(PerturbError) as excinfo:
        selector.select("blake2b:current", _rng(1))
    assert excinfo.value.code == "E-CACHE-005"


def test_stale_output_raises_when_only_the_current_key_is_in_history() -> None:
    """The current call's own key is excluded from the draw — if it is the only entry, miss."""
    history = RunHistory()
    history.record("k1", _response("first"))
    selector = StaleOutputSelector(history=history)
    with pytest.raises(PerturbError) as excinfo:
        selector.select("k1", _rng(1))
    assert excinfo.value.code == "E-CACHE-005"


def test_stale_output_excludes_the_current_key_and_is_reproducible_at_a_fixed_seed() -> None:
    """20 draws at the same seed select the same earlier entry every time (def. of done 3)."""
    history = RunHistory()
    history.record("k1", _response("first"))
    history.record("k2", _response("second"))
    history.record("k3", _response("third"))
    selector = StaleOutputSelector(history=history)
    results = [selector.select("k3", _rng(42)).source_cache_key for _ in range(20)]
    assert len(set(results)) == 1
    assert results[0] in {"k1", "k2"}


def test_stale_output_result_never_equals_the_perturbed_key() -> None:
    """Design constraint 4: a perturbation can never silently be the genuine answer."""
    history = RunHistory()
    history.record("k1", _response("first"))
    history.record("k2", _response("second"))
    selector = StaleOutputSelector(history=history)
    for seed in range(20):
        result = selector.select("k2", _rng(seed))
        assert result.source_cache_key != "k2"
        assert result.mode == "stale_output"


# ---------------------------------------------------------------------------------------
# contradictory
# ---------------------------------------------------------------------------------------


def _put(store: SqliteCacheStore, *, prompt: str, run_id: str) -> str:
    messages = [{"role": "user", "content": prompt}]
    key = cache_key_for("m", messages, {})
    response = dataclasses.replace(_response(f"answer to {prompt}"), recorded_run_id=run_id)
    store.put(
        key,
        key_version=KEY_VERSION,
        model="m",
        prompt_hash=response_hash_of(prompt),
        key_material=key_material_json("m", messages, {}),
        response=response,
        provider="openai_compatible",
        response_hash=response_hash_of(response.body),
    )
    return key


def test_contradictory_raises_when_pool_is_empty(tmp_path: Path) -> None:
    """`E-CACHE-005` — an empty declared pool has nothing eligible to serve."""
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    selector = ContradictoryPoolSelector(store=store)
    with pytest.raises(PerturbError) as excinfo:
        selector.select("blake2b:current", _rng(1))
    assert excinfo.value.code == "E-CACHE-005"
    store.close()


def test_contradictory_excludes_the_key_being_perturbed(tmp_path: Path) -> None:
    """The only stored entry is the key itself — still an empty *eligible* pool."""
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    key = _put(store, prompt="only one", run_id="r1")
    selector = ContradictoryPoolSelector(store=store)
    with pytest.raises(PerturbError):
        selector.select(key, _rng(1))
    store.close()


def test_contradictory_pool_run_id_filters_candidates(tmp_path: Path) -> None:
    """`pool_run_id` narrows the draw to entries recorded under that run only."""
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    _put(store, prompt="a", run_id="r_pool")
    _put(store, prompt="b", run_id="r_pool")
    _put(store, prompt="c", run_id="r_other")
    key = _put(store, prompt="current", run_id="r_pool")
    selector = ContradictoryPoolSelector(store=store, pool_run_id="r_pool")
    for seed in range(20):
        result = selector.select(key, _rng(seed))
        assert result.mode == "contradictory"
        assert result.source_cache_key != key
        entry = store.lookup_entry(result.source_cache_key)
        assert entry is not None
        assert entry.response.recorded_run_id == "r_pool"
    store.close()


def test_contradictory_is_reproducible_at_a_fixed_seed(tmp_path: Path) -> None:
    """20 draws at the same seed pick the same contradictory entry every time."""
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    _put(store, prompt="a", run_id="r1")
    _put(store, prompt="b", run_id="r1")
    key = _put(store, prompt="c", run_id="r1")
    selector = ContradictoryPoolSelector(store=store)
    results = [selector.select(key, _rng(7)).source_cache_key for _ in range(20)]
    assert len(set(results)) == 1
    store.close()


# ---------------------------------------------------------------------------------------
# confident_wrong
# ---------------------------------------------------------------------------------------


def test_confident_wrong_pool_load_rejects_a_missing_file(tmp_path: Path) -> None:
    """`E-CACHE-006` — the path does not exist."""
    with pytest.raises(PerturbError) as excinfo:
        ConfidentWrongPool.load(tmp_path / "does-not-exist.json")
    assert excinfo.value.code == "E-CACHE-006"


def test_confident_wrong_pool_load_rejects_invalid_json(tmp_path: Path) -> None:
    """`E-CACHE-006` — the file exists but is not valid JSON."""
    path = tmp_path / "pool.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(PerturbError) as excinfo:
        ConfidentWrongPool.load(path)
    assert excinfo.value.code == "E-CACHE-006"


def test_confident_wrong_pool_load_rejects_an_empty_array(tmp_path: Path) -> None:
    """`E-CACHE-006` — a syntactically valid but empty pool is still unusable."""
    path = tmp_path / "pool.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(PerturbError) as excinfo:
        ConfidentWrongPool.load(path)
    assert excinfo.value.code == "E-CACHE-006"


def test_confident_wrong_pool_load_rejects_a_missing_required_field(tmp_path: Path) -> None:
    """`E-CACHE-006` — an entry without `body` or `model` cannot be served."""
    path = tmp_path / "pool.json"
    path.write_text('[{"model": "m"}]', encoding="utf-8")
    with pytest.raises(PerturbError) as excinfo:
        ConfidentWrongPool.load(path)
    assert excinfo.value.code == "E-CACHE-006"


def test_confident_wrong_pool_loads_and_selects(tmp_path: Path) -> None:
    """A well-formed pool loads, and `select` never raises since it always has entries."""
    path = tmp_path / "pool.json"
    path.write_text(
        '[{"body": "wrong-1", "model": "m"}, {"body": "wrong-2", "model": "m", '
        '"prompt_tokens": 4, "completion_tokens": 4, "finish_reason": "stop"}]',
        encoding="utf-8",
    )
    pool = ConfidentWrongPool.load(path)
    assert len(pool.entries) == 2
    result = pool.select("blake2b:current", _rng(3))
    assert result.mode == "confident_wrong"
    assert result.response.body in {"wrong-1", "wrong-2"}
    assert result.source_cache_key.startswith(str(path))


def test_confident_wrong_pool_is_reproducible_at_a_fixed_seed(tmp_path: Path) -> None:
    """20/20 identical draws at a fixed seed (definition of done 3, literally)."""
    path = tmp_path / "pool.json"
    path.write_text(
        '[{"body": "a", "model": "m"}, {"body": "b", "model": "m"}, {"body": "c", "model": "m"}]',
        encoding="utf-8",
    )
    pool = ConfidentWrongPool.load(path)
    results = [pool.select("blake2b:current", _rng(99)).response.body for _ in range(20)]
    assert len(set(results)) == 1


# ---------------------------------------------------------------------------------------
# PerturbSelector — mode validation and dispatch
# ---------------------------------------------------------------------------------------


def test_perturb_selector_rejects_an_unknown_mode() -> None:
    """`E-CACHE-007` — no implicit fourth mode (design constraint 6)."""
    with pytest.raises(PerturbError) as excinfo:
        PerturbSelector(mode="not_a_real_mode", strategy=StaleOutputSelector(history=RunHistory()))
    assert excinfo.value.code == "E-CACHE-007"


def test_perturb_selector_dispatches_to_its_strategy() -> None:
    """`PerturbSelector.select` delegates to the wrapped strategy."""
    history = RunHistory()
    history.record("k1", _response("first"))
    selector = PerturbSelector(mode="stale_output", strategy=StaleOutputSelector(history=history))
    result = selector.select("k2", _rng(1))
    assert result.source_cache_key == "k1"
    assert result.mode == "stale_output"


def test_perturb_selector_rejects_a_mode_strategy_mismatch() -> None:
    """`E-CACHE-013` — `mode` and `strategy` must name the same perturbation kind.

    Regression test for a gap the independent review found: `__post_init__`'s docstring
    claimed to validate "mode matches the given strategy's shape," but the body only checked
    `mode in PERTURB_MODES` — so `PerturbSelector(mode="confident_wrong",
    strategy=StaleOutputSelector(...))` used to construct without error and would silently
    have run `StaleOutputSelector`'s behaviour under a `"confident_wrong"` label.
    """
    with pytest.raises(PerturbError) as excinfo:
        PerturbSelector(mode="confident_wrong", strategy=StaleOutputSelector(history=RunHistory()))
    assert excinfo.value.code == "E-CACHE-013"
