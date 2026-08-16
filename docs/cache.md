# The LLM record/replay/perturb cache — contract and error reference

> Companion to `docs/sdk.md` (the SDK's `LlmCache` Protocol and `E-CACHE-001`/`E-LLM-001`)
> and `docs/storage.md` (the event log store). Implements PRD §11. Every error code below is
> a stable part of the public contract: it appears in CI output, it is linked from error
> messages, and renumbering one is a breaking change.

---

## 1. What this module is, and what it is not

`runtime/cache/` is the persistence and mode-policy layer behind `RunContext.cache`
(`sdk/generic.py`'s `LlmCache` Protocol). It answers three questions for every model call the
SDK makes: *what does this call's cache key cover* (`key.py`), *where is a recorded response
stored, and how is a miss diagnosed* (`store.py`), and *what should this run's declared mode
do with a lookup or a write* (`modes.py`, `perturb.py`).

It is **not** the call site. `sdk/providers/openai_compatible.py` (P04, already built,
unmodified by this module) is the one place that calls `run.cache.lookup(key)` and
`run.cache.store(key, response)` — see §9 below for exactly what that boundary means for
this module's `store()` signature. It is not a provider client (P04's job) and it is not
analysis (out of scope, invariant I3's layer contract forbids `runtime/` from being imported
by `analysis/` in the direction that would matter here anyway).

---

## 2. The four modes (PRD §11.2), explicit and exhaustive

`Cache.mode` is always one of `agentdx.config.CACHE_MODES` — `"record"`, `"replay"`,
`"perturb"`, `"passthrough"` — the same tuple `[run] mode` in `agentdx.toml` validates
against. There is no fifth, implicit mode; `Cache.__post_init__` rejects anything else
(`E-CACHE-008`) before a single lookup happens.

| Mode | `lookup()` | `store()` |
|---|---|---|
| `record` | Returns the genuine stored response if one exists, else `None` (the SDK then makes a live call and stores it). | Writes. |
| `replay` | Returns the genuine stored response if one exists, else `None`. The SDK turns a `None` into `CacheMissError` (`E-CACHE-001`) and never falls back to a live call — invariant I7. | Refuses (`E-CACHE-010`) — a deterministic mode must never gain a new entry from a live call it should not have been able to make. |
| `perturb` | **Never the genuine response.** If the genuine call was recorded, returns a different response selected by a seeded `PerturbSelector` (design constraint 4, §6 below); if it was never recorded, `None` — perturbation substitutes for a real call, it does not invent one. | Refuses (`E-CACHE-010`), same reasoning as `replay`. |
| `passthrough` | Always `None`. The live SDK skips straight to a live call in this mode and never actually calls `lookup`, but a caller that does gets an honest miss rather than a stale hit from a mode that "does not consult the cache." | Writes. This is a known, declared SDK-side divergence from the PRD §11.2 prose ("passthrough does not consult the cache") — see §8, deviation 2. |

The mode never changes mid-run and is recorded in `run_start.payload.cache_mode` and the
scorecard (design constraint 6) — both owned by `sdk/generic.py`/`analysis/`, not this
module, but the value this module's `Cache.mode` was constructed with is the value that ends
up there. `events/schema.py`'s `run_start.payload` also carries a separate `mode` field (PRD
§6.1's *run* mode — `baseline`/`chaos`/`replay`/`explore`), explicitly documented as
"Distinct from cache_mode" — an earlier version of this section conflated the two.

---

## 3. Cache key construction (PRD §11.4, §11.5) — design constraint 1

The key is the `blake2b:` hash of the canonical JSON of:

```
{
  "model": <str>,
  "messages": <normalised messages>,
  "params": <significant params only>,
  "tools": <tool schemas, key-sorted>,
  "response_fmt": <response_format, or null>,
  "key_version": 2
}
```

### 3.1 What is in the key, and why

| In the key | Reason |
|---|---|
| `model` | A different model is a different experiment (PRD §11.5); the key changes whenever the model string changes, on purpose. |
| `messages` (normalised) | What was actually asked. Whitespace is stripped **only at message boundaries** (`" hello ".strip() == "hello"`), so a one-character prompt edit always changes the key and a trailing-whitespace formatting quirk never spuriously misses. Non-text content parts (images, audio) are represented by a content digest, not inlined — this is what keeps a multimodal key small and portable. |
| `params` (`SIGNIFICANT_PARAMS` only) | See §3.2. |
| `tools` | The tool schemas offered to the model. A different tool surface is a different question, even for an identical prompt. |
| `response_fmt` | A different requested output shape is a different call. |
| `key_version` | Lets the algorithm change (a field added, a normalisation rule tightened) without silently reinterpreting an old key as a new one — a version bump makes every existing entry a documented, explained miss instead of a wrong hit. Current value: **2**. |

### 3.2 `SIGNIFICANT_PARAMS` — the only params that participate

```
frequency_penalty, max_tokens, presence_penalty, seed, stop, temperature, tool_choice, top_p
```

Sorted, exhaustive. Nothing outside this set participates in the key, even if a caller passes
it — `key_material_for` reads only these eight names out of whatever mapping it is given.

### 3.3 What is deliberately excluded, and why

| Excluded | Reason |
|---|---|
| `user` | An arbitrary caller-supplied label; two calls that ask the same question for two different end users must still hit the same cache entry. |
| `stream` | Changes how the response is delivered, not what is asked. The cache always stores the full response either way (PRD §11.6). |
| Request timeouts | A wall-clock transport concern, invisible to the model. |
| Any machine-local salt | **Never.** PRD §11.4 is explicit, and this is what makes a cache portable between machines — the property `.agentdx` bundle import (PRD §11.9) depends on outright. |

### 3.4 Two implementations, proven equivalent for most values — proven to diverge for floats

`sdk/providers/openai_compatible.py` (P04) inlined this exact algorithm before
`runtime/cache/` (P07) existed, because the layer contract (`runtime/` must never import
`sdk/`) makes it impossible for this module to import that one. `runtime/cache/key.py` is a
second, independent implementation of the same PRD §11.4 algorithm — declared as a deviation
in `key.py`'s own module docstring, not left unmentioned — and
`tests/unit/cache/test_key.py::test_matches_the_sdk_implementation_byte_for_byte` proves the
two agree over 200 randomised calls (Hypothesis), including whitespace, empty strings, tools,
`response_format`, a `set`-valued `stop`, and an unkeyed `user` param.

**This equivalence does not hold wherever a `float` value appears in the material** — bare
(`temperature=0.7`) or nested inside a `set`/list/dict. `agentdx.events.canonical.encode_value`
(what this module hashes through) refuses raw floats outright (`FloatNotPermittedError`,
ruling R4); the SDK's own `stable_text` embeds a float as a bare, unquoted `repr()`. Both are
internally deterministic, but not the *same* string, so they hash differently —
`test_diverges_from_the_sdk_for_a_float_valued_significant_param` and
`test_a_float_nested_inside_a_set_still_diverges` assert this explicitly, rather than leaving
it untested by omission the way this module's first version's Hypothesis strategy did (it
never generated a float). **A response recorded through the real SDK for a call with a float
value anywhere in its significant params will not be found by this module's own diagnostic
tooling recomputing the key** — see NOT DONE/RISKS. Everywhere else — `model`, message text,
`bool`/`int`/`str`/`bytes`-valued params, and `set`s of non-float values — a response recorded
through the real SDK is found by anything that recomputes the key through this module (the
miss diagnostic, a future `agentdx cache` CLI, a bundle importer) — see §8 for why the
duplication was not resolved by editing the SDK file instead.

---

## 4. Storage (PRD §11.6, §11.9) — `SqliteCacheStore`

One SQLite file (`agentdx.toml`'s `[cache] db_filename`, default `cache.db`), one table:

```sql
CREATE TABLE llm_cache (
  cache_key         TEXT PRIMARY KEY,
  key_version       INTEGER NOT NULL,
  model             TEXT NOT NULL,
  prompt_hash       TEXT NOT NULL,
  key_material      TEXT NOT NULL,       -- additive, see §4.1
  response_body     TEXT NOT NULL,       -- TEXT, not zstd BLOB, see §4.1
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
CREATE INDEX idx_cache_model  ON llm_cache(model);   -- additive, see §4.1
```

`put()` is an **upsert**, not an append: a second `record`-mode write for the same
`cache_key` replaces the row rather than accumulating one, since a `cache_key` primary key
means there is exactly one current answer for one logical question. This is distinct from
"no automatic eviction" (§5) — nothing here removes an entry the caller did not name.

### 4.1 Three additive deviations from the literal PRD §11.6 DDL

1. **`key_material` (column).** The literal DDL stores `prompt_hash` but not the prompt
   itself, yet design constraint 2 requires a replay-mode miss to report "the closest stored
   key, and a diff of the two" — impossible from a hash alone, since hashes have no
   locality (a one-character edit produces a completely different digest). `key_material` is
   the canonical JSON text `key.key_material_json(...)` produces — the exact object
   `cache_key` hashes — so the diagnostic (§7) can render a real diff. This mirrors ADR-009's
   shape exactly (`events` gained `schema_version` for the same kind of reason: a downstream
   requirement needed a value the literal DDL omitted).
2. **`response_body` is `TEXT`, not a zstd-compressed `BLOB`.** ADR-008 already declined
   `zstandard` as a dependency; this module extends that ruling to its own response storage
   rather than re-raising a settled question. The value round-trips exactly either way.
3. **`idx_cache_model` (index).** Needed by the "closest stored key" diagnostic, which
   narrows its scan to same-model candidates first — a cross-model comparison is never
   useful (a different model is a different experiment, PRD §11.5).

None of these is a privacy regression: PRD §11.9 already treats the whole cache as
"necessarily holds bodies," gated by `0600` and bundle exclusion (§6 below) rather than by
withholding content — the prompt was always the more sensitive half of "bodies," and
`prompt_hash`'s mere presence already implied this file holds request-identifying data.

---

## 5. Invalidation (PRD §11.7) — explicit only, never automatic

**There is no automatic eviction.** `SqliteCacheStore.prune()` requires at least one of
`run_id`, `older_than_iso`, `model`; calling it with zero filters raises `E-CACHE-004` rather
than deleting everything. Silent, unfiltered eviction would break reproducibility of an old
bundle that still names entries this run wrote. `prune()` returns the number of rows deleted;
an unmatched filter deletes nothing and returns `0`, which is not an error.

A `key_version` bump (§3.1) is the other form of invalidation: `key_version` is part of every
key's material, so a bump changes every existing entry's hash outright — each becomes an
ordinary, explained miss rather than a silent wrong hit, without anyone having to call
`prune()` at all. This is **not**, today, a distinct diagnostic message of its own: `nearest()`
and `MissCandidate` (§7) do not carry a candidate's own `key_version`, so `describe_miss`
cannot compare it against the current one and call out a version bump specifically — a miss
caused by a `key_version` bump renders the same "closest stored key" text any other miss
would. An earlier version of this section claimed the miss message names the version mismatch
explicitly; it does not.

---

## 6. Privacy (PRD §11.9, §31.2, §31.4) — invariant I8

The cache file is created at `0600` (`CACHE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR`) on
first open and **re-verified, not merely requested, on every subsequent open**
(`_assert_private`, `E-CACHE-003` if it has been loosened to group- or world-readable). Its
`-wal`/`-shm` sidecars — which hold the same prompt/response bodies as the main file while a
transaction is in flight — are chmod'd to the same mode whenever SQLite has created them by
the time `open()` returns, and re-asserted private if either already exists on a reopen; an
earlier version of this module left them at the process umask's default permissions instead
(`tests/unit/cache/test_store.py::test_wal_and_shm_sidecars_are_private_while_the_store_is_open`
checks this while a store is still open, since SQLite deletes both sidecars on a clean
`close()`).

**No API key ever reaches this module.** API keys are read by `sdk/providers/*` and never
handed down to a cache implementation — there is nothing to redact there, because the absence
of an API-key parameter anywhere in this module's surface *is* the enforcement, not a runtime
check performed against one.

**Prompt/response bodies are a different question — PRD §11.9's `redact_patterns`.** "The
cache necessarily stores bodies... `redact_patterns` are applied to prompts before hashing and
storage; a redaction changes the key, which is correct — a redacted prompt is a different
prompt." `key.py`'s `normalise_messages`/`key_material_for`/`key_material_json`/
`cache_key_for` all accept an optional `redact: Callable[[str], str]` applied to message
*content* — string content and text-typed multimodal parts — after boundary whitespace is
stripped and before it is hashed (`tests/unit/cache/test_key.py`'s `test_redact_*` tests).
Scoped to content deliberately: `model`/`tools`/`response_format` are not prose and are not
where a secret plausibly appears, so redaction never touches them
(`test_redact_is_not_applied_to_the_model_name`).

**This is the capability, not the wiring.** The real, live SDK call site
(`sdk/providers/openai_compatible.py`, P04, out of this prompt's `DELIVERABLES`) computes its
own key inline and does not call `key.py` at all, so `redact_patterns` has zero effect on what
a live run's cache actually stores until that file is touched — the same shape of gap §4.1
already declares for `key_material`. Declared here, not silently implied to already be live
end to end.

**What "no plaintext bodies" actually governs, and where.** PRD §11.9 states the LLM cache
"necessarily holds bodies... replay requires it" — its privacy control is `0600` plus bundle
exclusion by default (§31.2, §31.3), never absence of content. NFR-6's actual "no plaintext"
acceptance criterion — "never write prompt/response bodies to the **event log** by default;
opt-in only [`capture_bodies`]... automated scan of the DB for plaintext after a default
run" — is about the *event log's* own database (`store/sqlite.py`, P03), not this one.
`tests/integration/cache/test_privacy_scan.py` demonstrates both halves directly: a default
(`capture_bodies=False`) run's event log contains neither the recorded prompt nor response
text anywhere in its bytes, while the LLM cache — correctly, intentionally — contains both,
protected by file permissions instead.

---

## 7. The design-constraint-2 miss diagnostic — `Cache.describe_miss`

A replay/perturb-mode miss is a hard error and **never an approximate match** — this
function only ever produces *text for an error message already being raised or logged*; it
never causes `lookup` to return anything other than `None`/the genuine mismatch. Given a
missed key (and, when the caller has it, the missed call's `key_material`), it renders:

1. the missed key and the active `key_version`/mode;
2. the closest stored key by Levenshtein edit distance over `key_material` text, scanning at
   most `scan_limit` (`agentdx.toml`'s `[cache] miss_diagnostic_scan_limit`, default 200)
   same-model entries, most recent first, so a large cache cannot make the message itself
   slow;
3. a real unified diff (`difflib.unified_diff`) of the two key materials, when both are
   known — not a description of a diff, an actual one;
4. up to `miss_diagnostic_candidates` (default 5) other close candidates;
5. the exact remedy: `agentdx run --record`.

**This is not wired into the live SDK's raised error today.** `sdk/providers/openai_compatible.py`'s
`_miss_message` (the text actually attached to `E-CACHE-001`) is fixed, tested P04 code and
out of this prompt's `DELIVERABLES` — editing it would be an undeclared scope expansion.
`describe_miss` is real, tested, working code any caller with more than the bare `LlmCache`
Protocol can already use (a future CLI, a richer run host); see NOT DONE/RISKS.

---

## 8. Deviations from this module's literal instructions

1. **Cache key duplicated, not imported, from the SDK (§3.4).** `sdk/providers/openai_compatible.py`
   inlined PRD §11.4 before this module existed; the layer contract forbids the reverse
   import. Proven equivalent by a 200-example property test rather than assumed.
2. **`passthrough` mode's `store()` writes.** PRD §11.2 says passthrough "does not consult
   the cache," but the live, unmodified `_resolve` calls `run.cache.store(...)`
   unconditionally after every live call, in every mode that reaches one. `Cache.store`
   therefore accepts the write in `passthrough` too, rather than silently dropping data a
   later `record`-mode run could have reused — dropping it would be a second, undeclared
   deviation stacked on the SDK's first, undeclared-by-this-module one.
3. **`Cache.store()`'s optional keyword extras.** The real SDK call site passes exactly two
   positional arguments (`cache_key`, `response`), so a row reached through it necessarily
   has no `key_material` to persist. `Cache.store` accepts optional, defaulted keyword-only
   `key_material`/`prompt_hash`/`model`/`provider` so a richer caller (this module's own
   tests, a future run host) can supply full provenance without breaking the two-positional
   call the live SDK actually makes. Entries recorded through the current, unmodified SDK
   wiring have an empty `key_material` and degrade `describe_miss`'s diff to "no comparison
   available" for those specific rows — see NOT DONE/RISKS.
4. **`SchedulerCacheHook` is registered at the interception point but the point does not
   fire.** See §9.

---

## 9. The scheduler injection point (PRD §11.3) — design constraint 3

`SchedulerCacheHook` structurally satisfies `runtime.scheduler.CacheHook` (one method,
`on_llm_yield(task_id, cache_key) -> int | None`) and implements the PRD §11.3 virtual-duration
chain: a real `by_kind` calibration entry wins first (an aggregated wall-clock profile beats
one call's own recording); failing that, the individual cache entry's own recorded
`duration_wall_ms`; failing that, the flat Q-43.2.3 default from `SchedulerConfig`. A cache
miss returns `None` — nothing to report.

**`runtime/scheduler.py` (P06, fixed) stores whatever `cache_hook` it is constructed with as
`self._cache_hook` and never calls `self._cache_hook.on_llm_yield` anywhere else** — confirmed
by `grep -n "_cache_hook\." src/agentdx/runtime/scheduler.py`, which finds only the
`__init__` assignment. This is the same shape as P06's own documented `RunHost` gap. It is a
real, declared, deliberately unresolved gap — this prompt's `AUTHORITATIVE INPUTS` name
`runtime/scheduler.py` as fixed and to be registered against, not modified — not an oversight
of this module. `tests/unit/cache/test_modes.py` exercises `on_llm_yield` by calling it
directly, and asserts (by source inspection, so it fails loudly rather than going stale) that
`runtime/scheduler.py` still has exactly one reference to `_cache_hook`.

---

## 10. Perturb mode (PRD §11.8) — design constraint 4

Perturb mode never *generates* a wrong answer — it **selects** one, deterministically, from a
pool that already exists. A generated "confidently wrong" response would itself require a
model call, which is exactly the non-determinism (I1) and analysis-path model dependency
(I13) this product refuses everywhere else.

Every selector takes an already-built `RandomSource` (structurally, one method:
`randrange(stop) -> int`) and never constructs its own; the caller owns the only random
state. Given the same seed and the same call sequence, selection is identical on every run —
`tests/unit/cache/test_perturb.py` asserts this literally: 20 draws at one seed select the
same perturbation 20/20, for all three modes.

| Mode | Selects from | What this module can resolve | What it cannot (yet) |
|---|---|---|---|
| `stale_output` | An earlier response served by *this `Cache` instance*, in this run. | Call order within one instance (`RunHistory`). | *Which agent* made each call — `LlmCache.lookup(cache_key: str)` carries no agent identity, and neither `scenario/` (P08) nor `runtime/faults/` (P09) exist to carry PRD §11.8's YAML `agent:` field down to this layer. Documented simplification, not full PRD §11.8 semantics. |
| `contradictory` | A response recorded for a *different* input, from a declared pool (`SqliteCacheStore`, filtered by `recorded_run_id` when a `pool:` is named). | The pool read and filter itself. | Parsing a scenario's `pool: run:r_9c113` YAML string into that filter — `scenario/`'s job (P08). |
| `confident_wrong` | A curated, hand-authored JSON file (`ConfidentWrongPool.load`). | Loading and selecting from a given path. | Reaching into `fixtures/perturbations/*.json` itself — that directory is P05's, and no file exists there yet; `load`'s own docstring documents the JSON schema this module expects, since PRD §11.8 names the directory but not a format. |

A perturbed selection always carries `source_cache_key` — the entry the substitute actually
came from — and is guaranteed never to equal the key being perturbed (`E-CACHE-005` if no
eligible substitute exists). This is what would populate
`llm_call.payload.perturbed_from_run`; the live SDK's `_emit` hardcodes that field to `None`
today (see NOT DONE/RISKS) — a second, independent instance of the same "not wired into the
live call path" shape as §9's scheduler hook.

---

## 11. Error codes

<a id="e-cache-001"></a>
### `E-CACHE-001` — cache miss in replay/perturb

Owned by `sdk/generic.py`/`docs/sdk.md` (PRD §36, exit code 3), not raised by this module —
`SqliteCacheStore.lookup`/`Cache.lookup` return `None` on a miss, exactly matching the
`LlmCache.lookup` contract, so the SDK's own tested miss-then-raise sequence is unchanged by
which cache implementation sits behind it. Listed here because `store.py`'s `CacheStoreError`
explicitly reserves the code and never raises it, and because this module's `describe_miss`
(§7) is the richer diagnostic this code's message could use once wired up.

<a id="e-cache-002"></a>
### `E-CACHE-002` — cache DB corrupt

PRD §36's exact reserved meaning, and now a real check rather than a repurposed code: raised
by `SqliteCacheStore` when a row's stored `response_hash` does not match
`hash_text(response_body)` (`lookup_entry`, and `iter_all` for a full-cache scan) — `put()` has
always computed and stored `response_hash` from the exact body it writes, so a mismatch means
the row was corrupted or hand-edited since it was recorded. Also raised when a stored column
value cannot be narrowed to its expected type (`_as_int`) — a schema-violating value found
while reading a row is corruption by the same PRD §36 definition. **Fix:** `agentdx cache
verify` (no such CLI command exists yet; the mechanism — a full `iter_all()` scan — already
does), then re-record. An earlier version of this module also raised this code for "the
database refused WAL mode," an environment/filesystem condition unrelated to whether any data
is wrong; that case is `E-CACHE-012` now.

<a id="e-cache-003"></a>
### `E-CACHE-003` — cache file is not private

An existing cache file, or its `-wal`/`-shm` sidecar, has been loosened to group- or
world-readable/writable (PRD §11.9, §31.2). **Fix:** `chmod 600 <path>` before reopening it.
Checked on every open, not only at creation.

<a id="e-cache-004"></a>
### `E-CACHE-004` — `prune()` called with no filter

`prune()` requires at least one of `run_id`/`older_than_iso`/`model`. **Fix:** name at least
one filter; there is no "prune everything" call by design (PRD §11.7).

<a id="e-cache-005"></a>
### `E-CACHE-005` — no eligible perturbation

A `stale_output` draw with no earlier call in this run's history, or a `contradictory` draw
whose declared pool (after excluding the key being perturbed) is empty. **Fix:** the scenario
needs an earlier call to draw from, or a non-empty declared pool.

<a id="e-cache-006"></a>
### `E-CACHE-006` — malformed `confident_wrong` pool file

The path does not exist, is not valid JSON, is not a non-empty array, or an entry is missing
a required `body`/`model` string field. **Fix:** see `ConfidentWrongPool.load`'s docstring
for the exact schema this module expects.

<a id="e-cache-007"></a>
### `E-CACHE-007` — unknown perturb mode

`PerturbSelector.mode` is not one of `PERTURB_MODES` (`stale_output`, `contradictory`,
`confident_wrong`). No implicit fourth mode (design constraint 6).

<a id="e-cache-008"></a>
### `E-CACHE-008` — unknown cache mode

`Cache.mode` is not one of `agentdx.config.CACHE_MODES`. No implicit fifth mode.

<a id="e-cache-009"></a>
### `E-CACHE-009` — perturb mode misconfigured

`mode="perturb"` was constructed without both `perturb` (a `PerturbSelector`) and
`perturb_rng` (a `RandomSource`). Both are required together; there is no partial perturb
configuration.

<a id="e-cache-010"></a>
### `E-CACHE-010` — write attempted in a deterministic mode

`Cache.store()` was called while `mode` is `replay` or `perturb`. Neither mode ever makes a
live call through the real SDK wiring — a replay-mode miss is `E-CACHE-001`, hard, before
`_resolve` could reach its live-call branch — so a `store()` call arriving in either mode
indicates a caller bypassing that contract. Refused loudly (I7): a deterministic mode
silently gaining a new entry is exactly the kind of change invariant I1 exists to make
impossible to do by accident.

<a id="e-cache-011"></a>
### `E-CACHE-011` — key material has no reproducible representation

A value offered to `key.py`'s key construction (a multimodal message part, a significant
param) has no reproducible `repr()` — its default `__repr__` embeds this process's memory
address, so hashing it would key the same logical call differently between processes,
contradicting PRD §11.4's "the key never contains a machine-local salt." Mirrors
`sdk/generic.py`'s `stable_text`/`E-INSTR-008`, which this module cannot import
(`runtime/` must not import `sdk/`) and so re-implements the same detection for. An earlier
version of this module instead silently hashed the address (`hash_text(repr(value))`),
making the cache key process-local for exactly this case — the bug an independent review
found and this code exists to fix. **Fix:** give the type a stable `__repr__`, or convert it
to a plain value (`str`/`int`/`bool`/`list`/`dict`) before passing it as message content or a
significant param.

<a id="e-cache-012"></a>
### `E-CACHE-012` — WAL mode refused

`SqliteCacheStore.open()`'s `PRAGMA journal_mode=WAL` did not report back `wal` — an
environment/filesystem condition (some network filesystems do not support WAL), not a data
integrity problem, which is why it is a distinct code from `E-CACHE-002` rather than folded
into it. **Fix:** open the cache on a filesystem that supports SQLite's WAL mode.

<a id="e-cache-013"></a>
### `E-CACHE-013` — perturb mode/strategy mismatch

`PerturbSelector`'s `mode` and `strategy` name two different perturbation kinds (e.g.
`mode="confident_wrong"` with a `StaleOutputSelector`). `__post_init__`'s docstring always
claimed to validate this; an earlier version of the body only checked `mode in
PERTURB_MODES` and let a mismatched pair construct silently, so a caller asking for one kind
of perturbation could have silently gotten another's behaviour. **Fix:** pair each `mode`
with its corresponding strategy type (`stale_output`→`StaleOutputSelector`,
`contradictory`→`ContradictoryPoolSelector`, `confident_wrong`→`ConfidentWrongPool`).

---

## 12. Configuration (`agentdx.toml` `[cache]`)

| Key | Default | Meaning |
|---|---|---|
| `db_filename` | `"cache.db"` | The SQLite file name under the run's data directory. |
| `miss_diagnostic_candidates` | `5` | How many "other candidates considered" `describe_miss` lists. |
| `miss_diagnostic_scan_limit` | `200` | How many same-model rows `nearest()` scans (most recent first) before stopping, bounding a miss message's cost on a large cache. |

**Live today, and where.** `SqliteCacheStore.nearest()` and `Cache.describe_miss()` both
accept an optional `config: CacheConfig`, deriving `limit`/`scan_limit`/`candidates` from
`config.miss_diagnostic_candidates`/`config.miss_diagnostic_scan_limit` when not overridden
per call — defaulting to `CacheConfig()`'s own values (`5`/`200`) when no config is passed,
unchanged from every caller that predates this wiring
(`tests/unit/cache/test_store.py::test_nearest_defaults_come_from_cache_config`). An earlier
version of this module declared `CacheConfig`/`agentdx.toml`'s `[cache]` section but nothing
anywhere read it — the two limit values were hardcoded independently in `store.py`/`modes.py`
instead, so changing `agentdx.toml` could never have changed anything. **`db_filename` remains
declared but unconsumed**: nothing in this module's own `DELIVERABLES` decides *where* a
cache file lives — that is a run-host/CLI concern this prompt does not build — so there is no
call site here for that field. Declared here rather than silently implied to be fully wired.

---

## 13. Rulings

- **R-C1.** A record-vs-replay canonical-log comparison is not this module's determinism
  contract. `events/schema.py`'s own `cache_status` field spec says so: "STABLE... G3
  compares replays with replays, never a record run with a replay." A record run's first
  call is `miss_recorded`; a replay of it is `hit` — both correct, and `cache_status` is a
  STABLE (in-canonical) field, so the two logs are expected to differ there. The I1/gate-G3
  property this module's tests demonstrate is replay-vs-replay: two independent replays of
  the same recorded run (same `run_id`, since PRD §6.1 derives it as a content hash of
  scenario/seed/cache) produce a byte-identical canonical log.
