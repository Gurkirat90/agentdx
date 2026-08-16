"""Mode-aware LLM cache: record / replay / perturb / passthrough (PRD §11.2, §11.3).

**Modes are explicit and exhaustive.** `Cache.mode` is always one of `agentdx.config.CACHE_MODES`
— the same tuple `[run] mode` and `RunConfig` already validate against, so there is exactly one
place in the codebase that spells the four names (`config.py`) and this module imports it
rather than repeating it. `Cache.__post_init__` rejects anything else before a single lookup
happens; there is no implicit fifth mode and no "unset" state.

**The `LlmCache` boundary this module implements.** `sdk/generic.py` declares:

```python
class LlmCache(Protocol):
    def lookup(self, cache_key: str) -> CachedResponse | None: ...
    def store(self, cache_key: str, response: CachedResponse) -> None: ...
```

`runtime/` must not import `sdk/` (CONTEXT.md §4), so `Cache` below satisfies this
**structurally** — same method names, same minimal required parameters — without importing
the Protocol. `sdk.providers.openai_compatible.OpenAICompatibleClient._resolve` (P04, already
built and tested) is the real, live call site: it calls `run.cache.lookup(key)` and
`run.cache.store(key, response)` with exactly two positional arguments each, and already
implements the full PRD §11.2 mode-branch (replay/perturb hard-error-on-miss, record
lookup-then-live-call, passthrough always-live) — see that module's docstring. **This class
does not re-implement that branch**; it implements what a `lookup`/`store` pair *means* in
each mode, which `_resolve` cannot see because it only ever passes an opaque key and a
response, never the mode-specific policy.

**Why `store()` never receives the key material.** `_resolve` calls `run.cache.store(key,
CachedResponse(...))` — two arguments, nothing else — so a stored row reached through the real
SDK wiring necessarily has no `key_material` to persist (design constraint 1's "closest stored
key" diagnostic needs it; PRD §11.6's DDL has no column for it that a two-argument call could
fill even if this module wanted to). `Cache.store` accepts optional, defaulted keyword-only
extras (`key_material`, `prompt_hash`, `model`, `provider`) precisely so a *richer* caller —
this module's own tests, a future run-host — can supply full provenance without breaking the
two-positional-argument call the live SDK actually makes. **Declared, not silently accepted**:
docs/cache.md §4 and this response's NOT DONE/RISKS both say plainly that entries recorded
through the current, unmodified SDK wiring will have an empty `key_material` and therefore
degrade the miss diagnostic to "no comparison available" for those specific entries.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Final

from agentdx.config import CACHE_MODES, CacheConfig
from agentdx.runtime.cache.key import KEY_VERSION, hash_text
from agentdx.runtime.cache.perturb import PerturbSelector, RunHistory
from agentdx.runtime.cache.store import CachedResponse, SqliteCacheStore
from agentdx.runtime.clock import CalibrationProfile, RandomSource

_DOCS: Final = "docs/cache.md"


class CacheModeError(RuntimeError):
    """A `Cache` was misconfigured, or asked to do something its mode forbids.

    Carries a stable `E-CACHE-0NN` code, in the same family `store.py`/`perturb.py` use.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


@dataclass(eq=False)
class Cache:
    """The `LlmCache`-shaped implementation the SDK is injected with (PRD §11.2).

    One instance per run — `RunContext.cache` — constructed with the run's declared mode.
    See the module docstring for exactly what `lookup`/`store` do in each mode and why the
    boundary is drawn where it is.
    """

    backing_store: SqliteCacheStore
    mode: str
    provider: str = "unknown"
    perturb: PerturbSelector | None = None
    perturb_rng: RandomSource | None = None
    run_id: str | None = None
    _history: RunHistory = field(default_factory=RunHistory, init=False)
    _last_perturb_source: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the mode and, for `perturb`, that a selector and RNG were both given.

        Raises:
            CacheModeError: `E-CACHE-008` `mode` is not one of `agentdx.config.CACHE_MODES`,
                or `E-CACHE-009` `mode == "perturb"` with no `perturb`/`perturb_rng`.
        """
        if self.mode not in CACHE_MODES:
            detail = f"unknown cache mode {self.mode!r}; known: {list(CACHE_MODES)}"
            raise CacheModeError("E-CACHE-008", detail)
        if self.mode == "perturb" and (self.perturb is None or self.perturb_rng is None):
            detail = "mode='perturb' requires both `perturb` (a PerturbSelector) and `perturb_rng`"
            raise CacheModeError("E-CACHE-009", detail)

    # ------------------------------------------------------------------
    # sdk.generic.LlmCache structural conformance
    # ------------------------------------------------------------------

    def lookup(self, cache_key: str) -> CachedResponse | None:
        """Return the response `cache_key` should serve in this run's mode, or `None`.

        Guarantees per mode (PRD §11.2):

        * `passthrough` — always `None`. The live SDK never actually calls this in
          passthrough mode (`_resolve` skips straight to a live call), but a caller that
          does gets an honest miss rather than a stale hit from a mode that "does not
          consult the cache."
        * `record` — the genuine stored response if one exists, else `None` (the caller
          then makes a live call and `store()`s it — PRD §11.2's record row).
        * `replay` — the genuine stored response if one exists, else `None`. The caller
          (the SDK) turns `None` into `E-CACHE-001` and never falls back to a live call —
          that hard-error behaviour lives in `sdk/providers/openai_compatible.py`, already
          built and unmodified by this module (I7, gate G9).
        * `perturb` — **never the genuine response.** If the genuine call was recorded,
          returns a *different* response selected by the seeded `PerturbSelector` (design
          constraint 4); if it was never recorded at all, `None` (perturbation substitutes
          for a real call, it does not invent one that was never made — PRD §11.8 perturbs
          "the same logical call").
        """
        if self.mode == "passthrough":
            return None
        genuine = self.backing_store.lookup(cache_key)
        if self.mode != "perturb":
            if genuine is not None:
                self._history.record(cache_key, genuine)
            return genuine
        if genuine is None:
            self._last_perturb_source = None
            return None
        assert self.perturb is not None  # noqa: S101 — enforced in __post_init__
        assert self.perturb_rng is not None  # noqa: S101
        result = self.perturb.select(cache_key, self.perturb_rng)
        self._last_perturb_source = result.source_cache_key
        self._history.record(cache_key, result.response)
        return result.response

    def store(
        self,
        cache_key: str,
        response: CachedResponse,
        *,
        key_material: str = "",
        prompt_hash: str = "",
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Persist `response` under `cache_key`, if this mode permits writing.

        Guarantees per mode:

        * `record`/`passthrough` — writes. `passthrough` writing is a known SDK-side
          deviation (see `docs/cache.md` §4): PRD §11.2 says passthrough "does not consult
          the cache," yet `_resolve`'s current code calls `store()` unconditionally after
          every live call, in every mode that reaches a live call at all. This module cannot
          change `_resolve` (P04, out of `DELIVERABLES`), so it accepts the write rather than
          silently dropping data the next `record`-mode run could have reused — dropping it
          would be a second, undeclared deviation stacked on the first.
        * `replay`/`perturb` — refuses. Neither mode ever makes a live call through the real
          SDK wiring (a replay-mode miss is `E-CACHE-001`, hard, before `_resolve` could
          reach its live-call branch), so a `store()` call arriving in either mode indicates
          a caller bypassing that contract — refused loudly rather than silently accepted,
          since a deterministic mode silently gaining a new entry is exactly the kind of
          change invariant I1 exists to make impossible to do by accident.

        Args:
            cache_key: The key this response answers.
            response: The response to store.
            key_material: The canonical JSON key material (`key.key_material_json(...)`),
                when the caller has it. Empty string when it does not — see the module
                docstring on why the real SDK call site cannot supply this.
            prompt_hash: `hash_text` of the prompt body, when the caller has it.
            model: Overrides `response.model` for the stored row's `model` column.
            provider: Overrides this cache's configured `provider` for this one row.

        Raises:
            CacheModeError: `E-CACHE-010` `mode` is `replay` or `perturb`.
        """
        if self.mode in ("replay", "perturb"):
            detail = (
                f"cache.store() called while mode={self.mode!r}; only record/passthrough "
                f"may write a cache entry (I7 — a deterministic mode must never gain a new "
                f"entry from a live call it should not have been able to make)"
            )
            raise CacheModeError("E-CACHE-010", detail)
        self.backing_store.put(
            cache_key,
            key_version=KEY_VERSION,
            model=model or response.model,
            prompt_hash=prompt_hash,
            key_material=key_material,
            response=response,
            provider=provider or self.provider,
            response_hash=hash_text(response.body),
        )
        self._history.record(cache_key, response)

    # ------------------------------------------------------------------
    # Diagnostics (design constraint 2) — not wired into the live SDK error path; see
    # docs/cache.md §7 and this response's NOT DONE/RISKS for exactly why.
    # ------------------------------------------------------------------

    def describe_miss(
        self,
        cache_key: str,
        *,
        key_material: str | None = None,
        model: str | None = None,
        config: CacheConfig | None = None,
        candidates: int | None = None,
        remedy: str = "agentdx run --record",
    ) -> str:
        """Render a design-constraint-2 hard-error message for a miss on `cache_key`.

        Guarantees: **never returns an approximate match as if it were a hit** — this
        function only ever produces text for an error message the caller is already raising
        (or logging); it never causes `lookup` to return something other than `None`/the
        genuine mismatch. The message names the key, the closest stored key by edit distance
        (design constraint 2), a real diff of the two key materials when both are known, and
        the exact remedy command.

        Args:
            cache_key: The key that missed.
            key_material: The missed call's own key material, for the closest-key diff.
            model: Restrict the candidate search to entries recorded for this model.
            config: Supplies `candidates`' default (`config.miss_diagnostic_candidates`) when
                it is omitted, and is passed straight through to `store.nearest` for its own
                `scan_limit` default — `agentdx.toml`'s `[cache]` section, PRD §11.4. Defaults
                to `CacheConfig()` (`5`) when not given, unchanged from every existing caller.
            candidates: Overrides `config`'s candidate count for this one call.
            remedy: The command line printed as the fix for this miss.
        """
        cfg = config or CacheConfig()
        effective_candidates = cfg.miss_diagnostic_candidates if candidates is None else candidates
        lines = [
            f"cache miss: key={cache_key[:24]}… key_version={KEY_VERSION} mode={self.mode!r}",
        ]
        near = self.backing_store.nearest(
            key_material or "", model=model, config=cfg, limit=effective_candidates
        )
        if not near:
            lines.append("no stored entries to compare against (this cache is empty).")
        else:
            best = near[0]
            lines.append(
                f"closest stored key: {best.cache_key[:24]}… "
                f"(model={best.model!r}, edit distance={best.distance})"
            )
            if key_material:
                lines.extend(_diff_lines(key_material, best.key_material))
            if len(near) > 1:
                rest = ", ".join(f"{c.cache_key[:16]}…({c.distance})" for c in near[1:])
                lines.append(f"other candidates considered: {rest}")
        lines.append(
            "a replay/perturb-mode miss is a hard error and never falls back to a live "
            f"call (I7). Re-record with: {remedy}"
        )
        return "\n".join(lines)

    @property
    def last_perturb_source(self) -> str | None:
        """Return the `cache_key` the most recent perturb-mode `lookup` substituted from.

        `None` until a perturbed lookup has happened, or after a perturb-mode miss. Exists
        so a caller with access to more than the bare `LlmCache` Protocol (this module's own
        tests, a future integration) can populate `llm_call.payload.perturbed_from_run`
        correctly — see docs/cache.md §4 on why the live SDK wiring cannot reach this today.
        """
        return self._last_perturb_source


def _diff_lines(missed: str, closest: str) -> list[str]:
    """Return a short unified-diff of two key-material texts, for a miss message."""
    diff = list(
        difflib.unified_diff(
            closest.splitlines(),
            missed.splitlines(),
            fromfile="closest stored key",
            tofile="this call",
            lineterm="",
            n=1,
        )
    )
    if not diff:
        return ["(the two key materials render identically once split into lines)"]
    return diff[:20]


# ---------------------------------------------------------------------------------------
# The scheduler injection point (design constraint 3, PRD §11.3)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchedulerCacheHook:
    r"""Virtual duration for a cached LLM call — the scheduler's `CacheHook` shape.

    **Registered at the interception point, but the interception point does not fire.** This
    class structurally satisfies `runtime.scheduler.CacheHook` — one method, `on_llm_yield`,
    same signature — and is what this prompt's `Scheduler(cache_hook=...)` should be
    constructed with. But `runtime/scheduler.py` (fixed; this prompt's `AUTHORITATIVE INPUTS`
    says "register at its interception points, do not modify it") stores whatever is passed
    as `self._cache_hook` and **never calls `self._cache_hook.on_llm_yield` anywhere** —
    confirmed by `grep -n "_cache_hook\\." src/agentdx/runtime/scheduler.py`, which finds
    only the assignment in `__init__`. PRD §11.3's "the scheduler asks the cache for the
    response" step 2 has no call site in the tree yet. This is the same shape as the
    `RunHost` gap P06's own OP-2 audit named root cause (d) — a real, declared, deliberately
    unresolved gap, not an oversight of this module's. See `docs/cache.md` §7 and this
    response's NOT DONE/RISKS. `tests/unit/cache/test_modes.py` calls `on_llm_yield` directly
    (see its `test_scheduler_never_actually_calls_on_llm_yield`, which also asserts this gap
    hasn't silently closed), since there is no live scheduler call site to exercise it
    through end to end.
    """

    store: SqliteCacheStore
    calibration: CalibrationProfile

    def on_llm_yield(self, task_id: str, cache_key: str) -> int | None:
        """Return the virtual duration for `cache_key`, or `None` if it is not cached.

        PRD §11.3's chain, minus the fault-injector branch (`runtime/faults/`, P09, does not
        exist): a real calibration entry for the `llm_call` kind wins first — it reflects an
        aggregated wall-clock profile rather than one call — and only when *no* real
        calibration entry exists does the individual cache entry's own recorded
        `duration_wall_ms` apply; the flat Q-43.2.3 default is the last resort. Checking "is
        there a *real* entry" against `CalibrationProfile.by_kind` rather than only calling
        `duration_for` matters: `duration_for` never returns `None` — `defaults_ms` always
        has an `llm_call` entry once a profile is built at all (`defaults_only` sets it from
        `SchedulerConfig.calibration_llm_ms`) — so composing on its return value alone would
        make the flat default silently shadow the recorded duration on every call in the
        common case where no calibration pass has ever been run.

        **Known narrowing**: `CalibrationProfile.by_group` keys on `(agent_id, kind, name)`,
        but this hook's fixed signature (`runtime.scheduler.CacheHook`, not modifiable here)
        receives only `task_id` and `cache_key` — no `agent_id`. `task_id` is passed in its
        place for the group lookup, which will rarely if ever match a real group entry
        (task ids and agent ids are different namespaces); this hook therefore effectively
        operates at the `by_kind`/recorded-duration/default granularity, not per-agent
        calibration groups, until the scheduler exposes `agent_id` to this call. Documented
        rather than silently narrowed.
        """
        entry = self.store.lookup_entry(cache_key)
        if entry is None:
            return None
        kind_entry = self.calibration.by_kind.get("llm_call")
        if kind_entry is not None:
            return self.calibration.duration_for(
                agent_id=task_id, kind="llm_call", name=entry.response.model
            )
        if entry.response.duration_wall_ms is not None:
            return entry.response.duration_wall_ms
        return self.calibration.duration_for(
            agent_id=task_id, kind="llm_call", name=entry.response.model
        )


__all__ = [
    "Cache",
    "CacheModeError",
    "SchedulerCacheHook",
]
