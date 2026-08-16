"""Perturb-mode response selection (PRD §11.8, design constraint 4).

Perturb mode never generates a wrong answer — it **selects** one, deterministically, from a
pool that already exists. That is the whole reason it exists instead of an LLM judge (PRD
§11.8's `[OPEN] §43.1.4` resolution, PRD §43.3): a generated "confidently wrong" response would
itself require a model call to produce, which is exactly the non-determinism (invariant I1)
and the analysis-path model dependency (invariant I13) this product refuses everywhere else.

**Seeded, never `random.*`.** This file is not on
`scripts/check_determinism_hygiene.py`'s `ALLOWLIST` and does not need an exemption: every
selector here takes a `RandomSource` (structurally `agentdx.runtime.clock.RandomSource` — one
method, `randrange(stop) -> int`) as an argument and never constructs its own. The caller —
eventually the scheduler's seeded run RNG, in tests a plain `random.Random(seed)` built outside
`src/agentdx/` — owns the only random state. Given the same seed and the same sequence of
calls, selection is identical on every run, which `tests/unit/cache/test_perturb.py` asserts
literally: 20 runs at one seed select the same perturbations 20/20 (definition of done 3).

**PRD §11.8's three modes, and what this module can and cannot resolve about them:**

* `stale_output` — "serve the response this agent gave at an earlier step of the same run."
  This module tracks *call order within one `Cache` instance* (`RunHistory`), not *which
  agent* made each call — the `LlmCache.lookup(cache_key: str)` boundary (PRD §11.2's
  Protocol, `sdk/generic.py`) carries no agent identity at all, and neither `scenario/` (P08)
  nor `runtime/faults/` (P09) — the modules that would carry PRD §11.8's YAML `agent:` field
  down to this layer — exist yet. `StaleOutputSelector` therefore selects from *every* earlier
  call this cache instance has served, which is a documented simplification, not the full PRD
  §11.8 semantics; narrowing it to one agent's own history is P08/P09's wiring job once a
  scenario can name a target agent.
* `contradictory` — "serve a response recorded for a different input in the declared pool."
  `ContradictoryPoolSelector` reads the declared pool directly from a `SqliteCacheStore`,
  filtered by `recorded_run_id` when the pool names one (`pool: run:r_9c113`), excluding the
  key being perturbed — exactly PRD §11.8's YAML shape, minus the scenario-file parsing that
  would resolve the `pool:` string, which belongs to `scenario/` (P08).
* `confident_wrong` — "serve from a curated pool of hand-authored wrong-but-plausible
  responses." `ConfidentWrongPool.load` reads a JSON file at a caller-given path (this module
  does not reach into `fixtures/` itself — `fixtures/` is P05's, and no `fixtures/perturbations/
  *.json` file exists yet to load; the schema this module expects is documented on `load`).

Every perturbed selection is returned as a `PerturbResult` carrying `source_cache_key` — the
entry the substitute actually came from — so the caller (see `modes.py`) can populate
`llm_call.payload.perturbed_from_run` and satisfy PRD §11.8's "no analysis can mistake a
perturbed response for a genuine one."
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agentdx.runtime.cache.store import CachedResponse, SqliteCacheStore
from agentdx.runtime.clock import RandomSource

_DOCS: Final = "docs/cache.md"

PERTURB_MODES: Final = ("stale_output", "contradictory", "confident_wrong")
"""PRD §11.8's `mode:` enum, exhaustive. No implicit fourth mode."""


class PerturbError(RuntimeError):
    """A perturbation could not be selected: an empty or misconfigured pool.

    Carries a stable `E-CACHE-0NN` code, in the same family `store.py` uses.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


@dataclass(frozen=True, slots=True)
class PerturbResult:
    """One selected substitute response, with the provenance PRD §11.8 requires be visible.

    Guarantees: `source_cache_key` never equals the key being perturbed — a perturbation
    that "substituted" the genuine answer for itself would silently defeat the whole point.
    """

    response: CachedResponse
    source_cache_key: str
    mode: str


@dataclass
class RunHistory:
    """The responses a `Cache` instance has served earlier in *this* run, in call order.

    Guarantees: append-only for the run's lifetime; `entries` is returned as a tuple copy so
    a caller cannot mutate the history a selector is reading from underneath it.
    """

    _entries: list[tuple[str, CachedResponse]] = field(default_factory=list)

    def record(self, cache_key: str, response: CachedResponse) -> None:
        """Note that `response` (for `cache_key`) was served, in order."""
        self._entries.append((cache_key, response))

    def entries(self) -> tuple[tuple[str, CachedResponse], ...]:
        """Return every recorded `(cache_key, response)` pair, oldest first."""
        return tuple(self._entries)


@dataclass(frozen=True, slots=True)
class StaleOutputSelector:
    """PRD §11.8 `mode: stale_output` — serve an earlier response from this run's history.

    See the module docstring: "earlier in this run" is exact; "this agent" is not narrowed,
    a documented simplification pending P08/P09.
    """

    history: RunHistory

    def select(self, cache_key: str, rng: RandomSource) -> PerturbResult:
        """Return a uniformly seeded-random earlier entry.

        Raises:
            PerturbError: `E-CACHE-005` no earlier call exists yet to draw from.
        """
        earlier = [(k, r) for k, r in self.history.entries() if k != cache_key]
        if not earlier:
            detail = (
                f"stale_output perturbation requested for {cache_key[:24]}… but this run "
                f"has no earlier recorded call to serve instead"
            )
            raise PerturbError("E-CACHE-005", detail)
        index = rng.randrange(len(earlier))
        source_key, response = earlier[index]
        return PerturbResult(response=response, source_cache_key=source_key, mode="stale_output")


@dataclass(frozen=True, slots=True)
class ContradictoryPoolSelector:
    """PRD §11.8 `mode: contradictory` — serve a response recorded for a different input.

    `pool_run_id`, when given, is the resolved form of the scenario YAML's
    ``pool: run:r_9c113`` (the ``run:`` parsing itself is `scenario/`'s job, P08); `None`
    means "any run recorded in this store."
    """

    store: SqliteCacheStore
    pool_run_id: str | None = None

    def select(self, cache_key: str, rng: RandomSource) -> PerturbResult:
        """Return a uniformly seeded-random entry from the declared pool, excluding `cache_key`.

        Raises:
            PerturbError: `E-CACHE-005` the pool has no eligible entry.
        """
        candidates = [
            entry
            for entry in self.store.iter_all()
            if entry.cache_key != cache_key
            and (self.pool_run_id is None or entry.response.recorded_run_id == self.pool_run_id)
        ]
        if not candidates:
            pool_desc = "any run" if self.pool_run_id is None else f"run {self.pool_run_id}"
            detail = (
                f"contradictory perturbation requested for {cache_key[:24]}… but the "
                f"declared pool ({pool_desc}) has no other recorded entry"
            )
            raise PerturbError("E-CACHE-005", detail)
        index = rng.randrange(len(candidates))
        chosen = candidates[index]
        return PerturbResult(
            response=chosen.response, source_cache_key=chosen.cache_key, mode="contradictory"
        )


@dataclass(frozen=True, slots=True)
class ConfidentWrongPool:
    """PRD §11.8 `mode: confident_wrong` — a curated, hand-authored pool of wrong responses.

    Guarantees: `entries` is immutable once loaded, so every run drawing from the same pool
    object sees the same candidate list regardless of draw order.
    """

    entries: tuple[CachedResponse, ...]
    source: str
    """Where this pool was loaded from — a file path, used as `source_cache_key`'s stand-in
    since curated entries have no real `cache_key` of their own."""

    @classmethod
    def load(cls, path: Path) -> ConfidentWrongPool:
        """Load a curated pool from a JSON file.

        **Schema** (this module's own — PRD §11.8 names the directory
        `fixtures/perturbations/*.json` but not a format): a JSON array of objects, each with
        the `sdk.generic.CachedResponse` fields — ``body`` (str, required), ``model`` (str,
        required), ``prompt_tokens``/``completion_tokens`` (int, default 0),
        ``finish_reason`` (str or null, default null). No ``duration_wall_ms`` or
        ``recorded_run_id``: a curated entry was never really recorded, and claiming a
        provenance run id for one would defeat design constraint 4 — perturbations must
        never be mistaken for genuine recordings.

        Raises:
            PerturbError: `E-CACHE-006` the file is missing, not JSON, not an array, empty,
                or an entry is missing a required field.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            detail = f"confident_wrong pool {path} does not exist"
            raise PerturbError("E-CACHE-006", detail) from exc
        except json.JSONDecodeError as exc:
            detail = f"confident_wrong pool {path} is not valid JSON: {exc}"
            raise PerturbError("E-CACHE-006", detail) from exc
        if not isinstance(raw, list) or not raw:
            detail = f"confident_wrong pool {path} must be a non-empty JSON array"
            raise PerturbError("E-CACHE-006", detail)
        entries = tuple(_response_from_dict(item, path) for item in raw)
        return cls(entries=entries, source=str(path))

    def select(self, cache_key: str, rng: RandomSource) -> PerturbResult:
        """Return a uniformly seeded-random curated entry."""
        index = rng.randrange(len(self.entries))
        return PerturbResult(
            response=self.entries[index],
            source_cache_key=f"{self.source}#{index}",
            mode="confident_wrong",
        )


def _response_from_dict(item: object, path: Path) -> CachedResponse:
    """Return one `CachedResponse` parsed from a `ConfidentWrongPool` JSON entry.

    Raises:
        PerturbError: `E-CACHE-006` the entry is not an object or is missing `body`/`model`.
    """
    if not isinstance(item, dict):
        detail = f"confident_wrong pool {path} has a non-object entry: {item!r}"
        raise PerturbError("E-CACHE-006", detail)
    body = item.get("body")
    model = item.get("model")
    if not isinstance(body, str) or not isinstance(model, str):
        detail = (
            f"confident_wrong pool {path} entry is missing a string 'body' or 'model': {item!r}"
        )
        raise PerturbError("E-CACHE-006", detail)
    finish_reason = item.get("finish_reason")
    return CachedResponse(
        body=body,
        model=model,
        prompt_tokens=int(item.get("prompt_tokens", 0) or 0),
        completion_tokens=int(item.get("completion_tokens", 0) or 0),
        finish_reason=None if finish_reason is None else str(finish_reason),
    )


PerturbStrategy = StaleOutputSelector | ContradictoryPoolSelector | ConfidentWrongPool

_STRATEGY_TYPE_FOR_MODE: Final = {
    "stale_output": StaleOutputSelector,
    "contradictory": ContradictoryPoolSelector,
    "confident_wrong": ConfidentWrongPool,
}
"""The one strategy type each `PERTURB_MODES` entry is paired with — `PerturbSelector`'s
`mode` is a label a caller chooses independently of `strategy`, so nothing in the type system
stops the two from disagreeing (`mode="confident_wrong"` with a `StaleOutputSelector`, say);
`__post_init__` is what actually enforces the pairing PRD §11.8 implies by naming exactly
three modes for exactly three strategies."""


@dataclass(frozen=True, slots=True)
class PerturbSelector:
    """Dispatches to one of PRD §11.8's three strategies by declared `mode`.

    One instance is built per run (or per `Cache`, see `modes.py`) from a scenario's
    declared fault — today, constructed directly by the caller, since `scenario/` (P08) and
    `runtime/faults/` (P09) do not exist yet to parse a YAML `faults:` block into one.
    """

    mode: str
    strategy: PerturbStrategy

    def __post_init__(self) -> None:
        """Validate `mode` is one of `PERTURB_MODES` and matches the given strategy's shape.

        Raises:
            PerturbError: `E-CACHE-007` `mode` is not a known perturb mode ·
                `E-CACHE-013` `mode` and `strategy` name two different perturbation kinds.
        """
        if self.mode not in PERTURB_MODES:
            detail = f"unknown perturb mode {self.mode!r}; known: {list(PERTURB_MODES)}"
            raise PerturbError("E-CACHE-007", detail)
        expected_type = _STRATEGY_TYPE_FOR_MODE[self.mode]
        if not isinstance(self.strategy, expected_type):
            detail = (
                f"mode={self.mode!r} requires a {expected_type.__name__} strategy, got "
                f"{type(self.strategy).__name__} — a PerturbSelector's mode and its strategy "
                f"must name the same one of PRD §11.8's three perturbation kinds, or a caller "
                f"asking for {self.mode!r} could silently get {type(self.strategy).__name__}'s "
                f"behaviour instead"
            )
            raise PerturbError("E-CACHE-013", detail)

    def select(self, cache_key: str, rng: RandomSource) -> PerturbResult:
        """Return the selected substitute for `cache_key`, per this selector's `mode`."""
        return self.strategy.select(cache_key, rng)


def load_confident_wrong_pools(paths: Sequence[Path]) -> tuple[ConfidentWrongPool, ...]:
    """Return every `ConfidentWrongPool` loaded from `paths`, in the given order."""
    return tuple(ConfidentWrongPool.load(p) for p in paths)


__all__ = [
    "PERTURB_MODES",
    "ConfidentWrongPool",
    "ContradictoryPoolSelector",
    "PerturbError",
    "PerturbResult",
    "PerturbSelector",
    "RunHistory",
    "StaleOutputSelector",
    "load_confident_wrong_pools",
]
