"""LLM record/replay/perturb cache and cache-key construction (PRD §11).

`replay` is the default mode and a replay-mode miss is a hard error (E-CACHE-001,
exit 3) — there is no fall-back-to-live flag and none may be added (I7).

**This package's actual boundary with the rest of the tree.** The hard-error-never-network
behaviour I7 requires is enforced by `sdk/providers/openai_compatible.py::_resolve` (P04,
already built, unmodified here) — it calls `run.cache.lookup(key)` and raises
`sdk.generic.CacheMissError` itself on `None` in `replay`/`perturb` mode. This package
supplies the four things that decision needed and did not have: a concrete `LlmCache`-shaped
store (`store.SqliteCacheStore`, `modes.Cache`), the PRD §11.4 key algorithm (`key.py`), seeded
perturbation selection (`perturb.py`), and the scheduler-side virtual-duration hook
(`modes.SchedulerCacheHook`) — registered as this prompt's `CacheHook` implementation, though
`runtime/scheduler.py` (fixed, not modified here) has no call site that invokes it yet. See
`docs/cache.md` for the full picture, including what is and is not wired end to end.

Modules:

* `key` — PRD §11.4/§11.5 cache key construction: what is in the key, what is deliberately
  out, and why for each exclusion.
* `store` — PRD §11.6/§11.9 SQLite-backed response storage, `0600` permissions, the
  "closest stored key" miss diagnostic (design constraint 2).
* `modes` — PRD §11.2/§11.3 record/replay/perturb/passthrough behaviour and the scheduler
  virtual-duration hook (design constraint 3).
* `perturb` — PRD §11.8 seeded, reproducible perturbation selection (design constraint 4).
"""

from agentdx.runtime.cache.key import (
    KEY_EXCLUDED_PARAMS_DOC,
    KEY_VERSION,
    SIGNIFICANT_PARAMS,
    cache_key_for,
    hash_text,
    key_material_for,
    key_material_json,
    normalise_messages,
    params_hash_for,
)
from agentdx.runtime.cache.modes import Cache, CacheModeError, SchedulerCacheHook
from agentdx.runtime.cache.perturb import (
    PERTURB_MODES,
    ConfidentWrongPool,
    ContradictoryPoolSelector,
    PerturbError,
    PerturbResult,
    PerturbSelector,
    RunHistory,
    StaleOutputSelector,
    load_confident_wrong_pools,
)
from agentdx.runtime.cache.store import (
    CACHE_FILE_MODE,
    CachedResponse,
    CacheStoreError,
    MissCandidate,
    SqliteCacheStore,
    StoredEntry,
)

__all__ = [
    "CACHE_FILE_MODE",
    "KEY_EXCLUDED_PARAMS_DOC",
    "KEY_VERSION",
    "PERTURB_MODES",
    "SIGNIFICANT_PARAMS",
    "Cache",
    "CacheModeError",
    "CacheStoreError",
    "CachedResponse",
    "ConfidentWrongPool",
    "ContradictoryPoolSelector",
    "MissCandidate",
    "PerturbError",
    "PerturbResult",
    "PerturbSelector",
    "RunHistory",
    "SchedulerCacheHook",
    "SqliteCacheStore",
    "StaleOutputSelector",
    "StoredEntry",
    "cache_key_for",
    "hash_text",
    "key_material_for",
    "key_material_json",
    "load_confident_wrong_pools",
    "normalise_messages",
    "params_hash_for",
]
