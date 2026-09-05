# OP-2 INDEPENDENT AUDIT (SECOND PASS) — P07 `runtime/cache/`

**Date:** 2026-09-05
**Module / prompt:** `runtime/cache/` (record / replay / perturb LLM cache), prompt P07
**Auditor:** independent OP-2 pass, fresh read, no memory of the original build or repair
**Prior history:** first independent OP-2 (2026-08-16) returned **FAIL** on 4 findings (key
material could silently key on a process memory address; `E-CACHE-002` overloaded across two
unrelated conditions; `response_hash` written but never verified on read; two tests with no
real discriminating power). Repaired same session (OP-3). **Never re-audited until now.**

**Scope.** `src/agentdx/runtime/cache/{key,store,modes,perturb}.py`,
`tests/unit/cache/*.py`, `tests/integration/cache/*.py`, plus the real, currently-shipped
consumers: `src/agentdx/cli/commands/run.py`, `src/agentdx/cli/host.py`,
`src/agentdx/runtime/scheduler.py` (its `CacheHook` interception point only), and
`src/agentdx/sdk/providers/openai_compatible.py`'s real call site.

---

## Method — what was actually executed, not just read

This sandbox is Python 3.10.12; the project is pinned `>=3.12,<3.13` and `agentdx/__init__.py`
unconditionally imports `agentdx.config`, which does `import tomllib` (D-66, standing,
confirmed project-wide). Rather than stopping at static reading, this audit built a disclosed,
throwaway, **never-committed** stdlib compatibility shim (`sitecustomize.py` under
`/tmp/audit/shim/`, added to `PYTHONPATH`) that backports exactly three 3.11+ stdlib names —
`tomllib` (aliased to the already-installed `tomli`), `datetime.UTC`, `enum.StrEnum` — with **no
agentdx logic reimplemented, patched, or altered**. This is the identical technique D-66 already
records a prior session using successfully. `pip3 install` turned out to work in this sandbox
(cached/reachable index) and pytest, hypothesis, httpx, pydantic and pytest-asyncio were
installed into the sandbox's own user site-packages — no repository file was touched.

This let the **real, unmodified, shipped test suite run for real**:

```
tests/unit/cache/        87 passed
tests/integration/cache/  7 passed
                          94 passed  -- matches the ledger's self-reported "94/94" exactly
```

Beyond the shipped suite, every finding below carries **live evidence against the real, imported
production classes** (`agentdx.runtime.cache.key.cache_key_for`,
`agentdx.runtime.cache.store.SqliteCacheStore`, `agentdx.runtime.cache.modes.Cache`) — not
reimplemented copies — via throwaway scripts under `/tmp/audit/`. Two findings are further
demonstrated by **in-process monkeypatch mutation** of the real, unmodified test functions
imported directly from `tests/unit/cache/test_modes.py`. No repository file was edited at any
point; `git status` was clean throughout.

Also read in full before touching the module: `CONTEXT.md` (all 495 lines), `AGENTS.md` (129
lines), PRD §11 (11.1–11.9) and §36's cache error rows, `docs/cache.md` (495 lines), and the
squashed original build+audit+repair commit `d1afbbf` (`git show --stat`, since no standalone
`op2-audit-p07.md`/`op3-repair-report-p07.md` exists at repo root or in `docs/journal/` — the
whole P07 narrative lives only in that one commit's message).

---

## VERDICT: **FAIL**

The two most consequential original findings (`E-CACHE-002` corruption detection actually
firing; the two "blind" tests) **hold up** under live adversarial re-test — see the "What holds
up" section. But two of the four re-tested repairs have **real, live-demonstrated gaps**, and
one previously-known limitation turns out to have a newly-introduced, undisclosed, dead-code
artifact sitting directly on top of it:

1. **(HIGH)** The key-material "no reproducible representation" guard (`E-CACHE-011`,
   `KeyMaterialError`) only catches CPython's *default* `__repr__` shape. A custom `__repr__`
   that embeds the same non-reproducible process-local `id()` in any other textual form sails
   straight through, unblocked, all the way into a real, shipped cache key — demonstrated live
   against the actual `cache_key_for`.
2. **(MEDIUM)** `SqliteCacheStore._as_int` can raise a raw, unhandled `ValueError` instead of
   the documented `CacheStoreError("E-CACHE-002", ...)` for a corrupted/hand-edited integer
   column whose value is a non-numeric string — violating both the class's own stated guarantee
   ("every method either succeeds or raises `CacheStoreError`") and `docs/cache.md`'s own
   explicit claim about this exact code path. Demonstrated live against the real
   `SqliteCacheStore.lookup_entry()` with a genuine, hand-tampered SQLite row.
3. **(HIGH)** `runtime/scheduler.py`'s `CacheHook` interception point (PRD §11.3's
   fault→calibration→cached-duration→default virtual-duration chain) is still, confirmed live
   and via `git blame`, **never called anywhere**, unchanged since P07's original build, despite
   the entire ADR-017→ADR-022 scheduler rewrite. This part is already disclosed in
   `docs/cache.md`/`docs/cli.md` in general terms, so it is **reconfirmed, not newly found** — but:
4. **(MEDIUM, new, previously undisclosed anywhere)** `cli/commands/run.py:335` — added at P17
   (commit `573e96e`, 2026-08-27, five months after P07's own audit closed) — calls
   `build_cache_hook(cache=cache, config=config)` and **discards the return value**. The real
   `Scheduler(...)` construction three lines earlier (`run.py:305-312`) has no `cache_hook=`
   argument at all, and is built *before* `cache`/`build_cache_hook` even exist in that function.
   This line does nothing except allocate a `SchedulerCacheHook` and immediately drop it — dead
   code that reads, on a skim, like the wiring gap has been addressed, when it has not been
   touched at all.

None of findings #1, #2, or #4 is restated from the original 2026-08-16 audit or its repair; all
three are demonstrated fresh below. Finding #3's underlying fact was already known; its
continued truth after five ADRs of scheduler surgery, and its interaction with finding #4, were
not previously re-checked.

---

## FINDING #1 (HIGH) — `KeyMaterialError`'s address-repr detector is a pattern match, not a proof, and a differently-spelled non-reproducible `repr()` sails through into a real cache key

**Where:** `src/agentdx/runtime/cache/key.py:123` (`_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")`)
and `:186-207` (`_reproducible_repr`), reached from `_as_payload_value`'s final fallback branch
(`:265`) and `_normalise_part`'s final fallback branch (`:234`) — i.e. every message-content
part and every non-`SIGNIFICANT_PARAMS` nested value that is not itself a primitive, mapping,
sequence, set, bytes, or float.

**What the original finding was, and what the repair actually did.** The pre-repair code fell
back to `hash_text(repr(value))` for any value outside a closed set of types; a plain object's
*default* `__repr__` (`<ClassName object at 0x...>`) embeds this process's memory address, so
the "same" logical call produced a different key in a different process. The repair added
`_reproducible_repr`, which runs `repr(value)` through the regex `r" at 0x[0-9a-fA-F]+"` and
raises `KeyMaterialError("E-CACHE-011", ...)` if it matches — i.e. it detects **one specific
textual shape** a non-reproducible repr can take, not the underlying property ("this repr's
content depends on this process's memory layout") that actually makes it unsafe.

**The adversarial re-break.** A class with a hand-written `__repr__` that embeds `id(self)` in
any format other than CPython's own `"... at 0x..."` — decimal, a different label, a different
prefix — is semantically identical to the original bug (its output is still a
process-local/allocator-local memory address) but does not match the regex.

**Live evidence, against the real, imported, unmodified `agentdx.runtime.cache.key.cache_key_for`:**

```python
class _CustomNonReproducibleRepr:
    def __repr__(self):
        return f"CustomObj(instance_id={id(self)})"

messages = [{"role": "user", "content": [{"type": "blob",
                                           "payload": _CustomNonReproducibleRepr()}]}]
cache_key_for("m", messages, {})
# -> 'blake2b:d63d6d740caf6ce45467def3c9e0c8a28e6cbce662682f84a0871fddfec2ef5d'
# No KeyMaterialError. The default-repr case (_NoReproducibleRepr, the shipped test's own
# fixture) *does* raise E-CACHE-011 in the same script -- only the custom-repr shape bypasses it.
```

A second run proves the practical consequence both ways, live, against the real function:

* **Cross-call divergence** (the original bug's own framing — "differs between processes for
  the identical logical call"): two *simultaneously alive* instances of the same class (so
  CPython cannot recycle one's address for the other) produce **two different cache keys**
  (`d18bdfce...` vs `705089d6...`) for what is, semantically, the identical logical call shape.
* **Cross-call spurious collision** (the sharper, previously-unconsidered consequence): when the
  two instances' lifetimes do *not* overlap — the ordinary case, since a caller typically builds
  one message, uses it, and lets it go before building the next — CPython's allocator reliably
  reused the freed object's address for the next one in this run, and the two calls produced the
  **identical** cache key (`8dd9ee1f...` both times) despite being two different logical calls.
  This is worse than the divergence case: a false *hit* silently serves the wrong recorded
  response for a genuinely different call, rather than merely failing to find a genuine one.

**Why this is reachable, not contrived.** The type signature this code accepts is
`Mapping[str, object]` (message content parts, tool schemas, `response_format`) — the module's
own docstring says as much: "a multimodal part, a tool-call artefact" are the named examples of
what legitimately reaches this fallback. Nothing stops a caller — a future multimodal provider
shim, a tool that returns a rich object — from passing a value with a hand-written `__repr__`
that is well short of the CPython default shape.

**Suggested fix direction.** Stop trying to pattern-match "looks like a memory address" in
arbitrary `repr()` output — that is an unbounded, adversarial-input detection problem with no
complete regex. Invert the policy: the closed set already enumerated in `_as_payload_value`
(`None`/`bool`/`int`/`str`/`float`/`bytes`/`bytearray`/`Mapping`/`Sequence`/`set`/`frozenset`) is
the *only* set of types with a provably reproducible representation. Drop the `repr()` fallback
entirely and raise `KeyMaterialError` unconditionally for anything outside that set, regardless
of what its `repr()` happens to contain. This is strictly more conservative, costs nothing for
any currently-passing test (none of the 94 rely on the fallback succeeding for a non-listed
type), and cannot be defeated by a differently-spelled non-reproducible `__repr__` because it no
longer inspects `repr()` content at all.

**Related, out of P07's own scope but worth a cross-reference:** the identical
`_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")` pattern and the identical detection strategy exist
in `src/agentdx/sdk/generic.py:74` (`stable_text`, raising `ValueRepresentationError`/
`E-INSTR-008`) — `key.py`'s own module docstring says it "mirrors" that function. The same
bypass almost certainly applies there too (not independently re-verified live in this audit,
since `sdk/` is outside P07's module boundary), which would mean a user graph's own state values
carry the identical I1 risk on the SDK's state-recording path, not just the cache-key path.

---

## FINDING #2 (MEDIUM) — `SqliteCacheStore._as_int` can leak a raw `ValueError`, contradicting the class's own guarantee and `docs/cache.md`'s explicit claim

**Where:** `src/agentdx/runtime/cache/store.py:551-567` (`_as_int`), reached from
`_entry_from_row` (`:575-634`) for `prompt_tokens`/`completion_tokens`/`duration_wall_ms` on
every `lookup`, `lookup_entry`, and `iter_all` call.

```python
def _as_int(value: object, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)          # <-- unguarded; raises ValueError for a non-numeric string
    detail = f"expected an integer column value, got {type(value).__name__}: {value!r}"
    raise CacheStoreError("E-CACHE-002", detail)
```

`docs/cache.md` §11's `E-CACHE-002` entry states, without qualification: *"Also raised when a
stored column value cannot be narrowed to its expected type (`_as_int`) — a schema-violating
value found while reading a row is corruption by the same PRD §36 definition."* This is true for
a value that is some other Python type (float, list, dict, `None`-already-handled) reaching the
final `else` branch — but a **string that is not a valid integer literal** never reaches that
branch at all: `int(value)` raises `ValueError` directly, which is not `CacheStoreError` and is
not caught anywhere in this method.

**Live evidence, against the real, imported, unmodified `SqliteCacheStore`:**

```python
store = SqliteCacheStore.open(db_path)
# ... put() a genuine, well-formed row via the real key.py/store.py APIs ...
store.close()

# Hand-tamper prompt_tokens via a raw sqlite3 UPDATE -- SQLite's INTEGER affinity does not
# reject a string it cannot convert; it stores it as TEXT.
conn.execute("UPDATE llm_cache SET prompt_tokens = 'abc' WHERE cache_key = ?", (key,))
conn.commit()

store2 = SqliteCacheStore.open(db_path)
store2.lookup_entry(key)
# -> ValueError("invalid literal for int() with base 10: 'abc'")
#    NOT CacheStoreError. Confirmed against the real, live, shipped class.
```

`SqliteCacheStore`'s own class docstring (`store.py:292-298`) states as a guarantee: *"every
method either succeeds or raises `CacheStoreError` — no method returns a sentinel that could be
confused with a genuine miss."* This case violates that guarantee for real, live code, not a
hypothetical.

**Blast radius, honestly assessed.** No call site anywhere in `src/agentdx/` currently catches
`CacheStoreError` specifically (confirmed via `grep -rn "CacheStoreError" src/agentdx` — every
match is either the class definition, a `raise` site, or a docstring), so today this does not
observably misroute error handling anywhere live — both exception types currently propagate as
an uncaught crash either way. But `store.py`'s own docstring for `iter_all` (`:524-528`) says
this exact mechanism "doubles as `agentdx cache verify`'s underlying mechanism, row by row, even
before that CLI command exists" — when that command is built, it will plausibly want to catch
`CacheStoreError` specifically to report "N corrupt rows found" cleanly, and this bug means a
non-numeric-string corruption of an integer column would instead crash that command outright.

**Also worth noting, not a separate finding:** the `E-CACHE-002` reuse between "hash mismatch"
(genuine PRD §36 corruption) and "wrong column type" (`_as_int`'s explicit `else` branch) is
**declared, not hidden** — `docs/cache.md` names both under the same code deliberately. This is
a different, and defensible, situation from the original finding's `E-CACHE-002`/WAL-refusal
conflation (which was undeclared and has since been correctly split into `E-CACHE-012`). The
`ValueError` leak above is the real defect; the shared code for the two *documented* conditions
is a judgment call, not a bug.

**Suggested fix direction.** Wrap the `isinstance(value, str): return int(value)` branch in a
`try/except ValueError`, raising the same `CacheStoreError("E-CACHE-002", ...)` the final
`else` branch already raises, with the string's own repr in the detail message. One test
(`tests/unit/cache/test_store.py`, alongside the existing hand-edit corruption tests) — tamper
`prompt_tokens` to a non-numeric string, assert `CacheStoreError` with code `E-CACHE-002` — would
close this and give the docs' own claim real coverage; none currently exists.

---

## FINDING #3 (HIGH, reconfirmed) — PRD §11.3's virtual-duration chain is still completely dark; the interception point has never fired, unchanged across the entire ADR-017→ADR-022 scheduler rewrite

**Where:** `src/agentdx/runtime/scheduler.py:28-30` (module docstring, unchanged since P06):
*"Out of scope (P09 and P07). The `FaultInjectorHook` and `CacheHook` protocols are defined here
as empty hook contracts so P09/P07 have a stable interface to implement; their bodies are not
built."* Class `CacheHook` at `:325-333`; constructor parameter `cache_hook` at `:517`; the one
and only reference, `self._cache_hook = cache_hook or CacheHook()`, at `:539`.

**What PRD §11.3 actually specifies** (`docs/AgentDX-PRD-v2.md:1849-1856`): the scheduler must
derive a cached LLM call's *virtual* completion duration from, in order: (a) the fault injector,
if a `latency`/`agent_slow` fault applies; (b) the calibration profile for the call group; (c)
the recorded duration stored alongside the cached response; (d) the documented flat default.
This is exactly what `runtime.cache.modes.SchedulerCacheHook.on_llm_yield`
(`modes.py:298-358`) implements, correctly, with real unit-test coverage of all four
priority branches (`test_modes.py:306-380`) — the logic is sound. It is simply never invoked.

**Live confirmation.** `grep -o "_cache_hook" src/agentdx/runtime/scheduler.py | wc -l` → `1`
(the assignment only — no call site). This exact count is also what
`tests/unit/cache/test_modes.py::test_scheduler_never_actually_calls_on_llm_yield`
(`:382-405`) asserts by source inspection, and it still passes today — confirmed as part of the
94/94 live pytest run above, and independently by the grep. **This is disclosed, not hidden**:
`docs/cache.md` §8 item 4 and §9 (`:291-313`) both name it explicitly, and `docs/cli.md`
(`:189-197`) separately notes the consequence — `achieved_speedup` prints a real, honest `0.00x`
in `compare --baseline`/`analyze --scorecard` because *no* span, cache-derived or otherwise,
ever gets a calibrated duration from the live scheduler.

**Why this is being re-flagged rather than treated as settled.** The task brief for this audit
specifically asked whether the scheduler's substantial ADR-017 through ADR-022 rewrite (identity
collision fixes, fan-out dispatch, root-entry drain, `_backgrounded` staleness — none of them
scoped to `CacheHook`) had, as a side effect, ever wired this in. It has not — `grep -rn
"Scheduler(" src/agentdx --include="*.py"` finds exactly one real construction site in the whole
tree (`cli/commands/run.py:305`), and it does not pass `cache_hook=`. The severity here is
carried by how central PRD §11.3 is to the product's own thesis (simulating realistic
concurrency and comparative timing cheaply, per §1's "why the time, tokens and correctness
went") and by the fact that it is not merely un-VERIFIED but has had zero forward progress
across five scheduler-focused ADRs that touched the exact file this hook lives in.

**Suggested fix direction.** Unchanged from what `docs/cache.md`/`docs/cli.md` already point at:
wire `Scheduler(cache_hook=build_cache_hook(cache=cache, config=config))` for real (see Finding
#4 for the concrete, current blocker to doing that in `cli/commands/run.py` specifically), then
add `Scheduler._resume_task`'s LLM-yield branch calling `self._cache_hook.on_llm_yield(...)`
and scheduling completion at `now + duration` per PRD §11.3 step 3. This is a `runtime/`-layer
change, outside `runtime/cache/`'s own `DELIVERABLES` — correctly not this module's own repair
to make unilaterally, same precedent as the P06 `RunHost` gap.

---

## FINDING #4 (MEDIUM, new — not previously documented anywhere) — `cli/commands/run.py` calls `build_cache_hook()` and silently discards it; the `Scheduler` is built before the cache even exists

**Where:** `src/agentdx/cli/commands/run.py:305-335`.

```python
scheduler = Scheduler(
    run_id=run_id, seed=seed, clock=clock, writer=writer,
    config=config.scheduler,
    fault_hook=armed.hook if armed is not None else None,
)                                                              # <- no cache_hook= here
scheduler_box.append(scheduler)
cache = build_cache(                                           # <- cache built AFTER the
    cache_db_path=..., mode=cache_mode, run_id=run_id,          #    Scheduler already exists
)
host = CliRunHost(..., cache=cache)
build_cache_hook(cache=cache, config=config)  # wired for a future scheduler cache_hook call
```

**What this line actually does: nothing.** `build_cache_hook` (`cli/host.py:509-517`) is a plain
constructor call with no side effects (`SchedulerCacheHook` is `@dataclass(frozen=True,
slots=True)`), and its return value here is not assigned to any name, not passed to `scheduler`,
not stored on `host` — it is built and immediately garbage-collected. Confirmed exhaustive via
`grep -rn "build_cache_hook(\|cache_hook="  src/agentdx`: the only two call sites for the
*function* are its own definition and this one discarded call; the only two mentions of the
*keyword* are a docstring reference (`modes.py:304`, prose) and `build_cache_hook`'s own
docstring (`host.py:510`, also prose) — the real `Scheduler(...)` construction three lines above
has no `cache_hook=` keyword argument anywhere in the tree.

**Why this matters beyond Finding #3's already-disclosed general gap.** `git blame` places this
exact line at commit `573e96e` (2026-08-27, P17's CLI build) — five months after P07's own
audit/repair closed (2026-08-16) and well after `docs/cache.md` was last touched for this topic.
It is mentioned **nowhere**: not in `CONTEXT.md`, not in `docs/cache.md`, not in
`op2-audit-p17.md`/`op3-repair-report-p17.md` (which focused on `--json`/`--assert`/`compare`
gaps, not this). The one place that comes close, `docs/cli.md:189-197`, describes the general
situation as "`CalibrationProfile.defaults_only`/`SchedulerCacheHook` are both built and unused
by the live scheduler, the same 'wired... but never called' shape" — language that reads as
"the object reaches the scheduler and its method is simply never invoked" (Finding #3's actual
shape, true of `scheduler.py`'s own `self._cache_hook`). What is actually happening in
`run.py:335` is a different and, in one respect, worse shape: the hook object never reaches the
`Scheduler` at all — it is constructed for no reason and discarded on the same line. A future
engineer skimming `run.py` and seeing a call literally named `build_cache_hook(cache=cache,
config=config)` sitting right next to the real `CliRunHost(...)` construction has a reasonable
chance of concluding the wiring gap has been addressed; it has not, and the current code
ordering (`Scheduler` built at line 305, `cache` not built until line 314) means the wiring
*cannot* be completed as a one-line change — the two constructions would need reordering first,
a detail no existing document names.

**Suggested fix direction.** Either (a) delete the discarded call entirely — it does nothing and
its presence is actively misleading — and leave Finding #3's existing, honest disclosure as the
single source of truth for this gap until `runtime/` is ready to consume it, or (b) do the small
reorder this fix actually needs (`cache = build_cache(...)` before `scheduler = Scheduler(...)`,
then pass `cache_hook=build_cache_hook(cache=cache, config=config)` into the `Scheduler(...)`
call) as the first half of closing Finding #3 for real. Either is better than the current
state, which is neither.

---

## What was checked and holds up

* **`response_hash` verification (`E-CACHE-002`, primary/documented case) — holds.**
  `_entry_from_row` genuinely recomputes `hash_text(response_body)` and raises
  `CacheStoreError("E-CACHE-002", ...)` on mismatch, on every `lookup_entry`/`iter_all` call —
  confirmed via the real, live, shipped `SqliteCacheStore` against a genuinely hand-tampered
  on-disk SQLite row (`UPDATE llm_cache SET response_body = ...`), and via the shipped
  `tests/unit/cache/test_store.py::test_a_hand_edited_response_body_fails_integrity_verification`
  / `test_iter_all_also_verifies_integrity`, both real (unmocked SQLite), both passing live.

* **`E-CACHE-012`/`E-CACHE-002` split (WAL-mode-refused vs. corruption) — holds.** Confirmed by
  reading `_open_connection` and by `test_wal_mode_refused_is_a_distinct_code_from_corruption`,
  which passed live.

* **The two originally "blind" tests now have real discriminating power — holds, live-verified
  by mutation, not just re-read.**
  * `test_perturb_lookup_never_returns_the_genuine_response`: imported the real, unmodified test
    function from `tests/unit/cache/test_modes.py` and ran it twice via in-process monkeypatch
    of the real `Cache.lookup` — once unmodified (passes) and once with a single-line mutation
    (`return genuine` instead of `return result.response` in the perturb branch, i.e. perturb
    mode silently serving the real answer). The mutated version **fails** the real test with a
    real `AssertionError`.
  * `test_replay_lookup_miss_returns_none_not_an_approximate_match`: same technique against
    `SqliteCacheStore.lookup_entry`, mutated to fall back to `nearest()`'s single closest
    candidate on an exact miss. Real test passes against the shipped code, **fails** against the
    mutation.

* **`PerturbSelector.__post_init__`'s mode/strategy validation (`E-CACHE-013`) — holds.**
  `test_perturb_selector_rejects_a_mode_strategy_mismatch` passed live; read the code and
  confirmed the check is a real `isinstance` comparison against `_STRATEGY_TYPE_FOR_MODE`, not a
  docstring-only claim.

* **`-wal`/`-shm` sidecar `0600` enforcement — holds.** Read `_open_connection`/`_chmod_private`;
  both sidecars are chmod'd on every open, not only the main file; the corresponding tests
  passed live.

* **`CacheConfig` (`miss_diagnostic_candidates`/`miss_diagnostic_scan_limit`) live-wiring into
  `nearest()`/`describe_miss()` — holds.** Confirmed by reading and by
  `test_nearest_defaults_come_from_cache_config` passing live.

* **Redaction (PRD §11.9) — the module-level capability holds** (`normalise_messages`'s
  `redact` parameter genuinely changes the key and genuinely changes what gets hashed, all six
  redaction tests passed live) — **but it is still not reachable from the real, live SDK call
  site** (`sdk/providers/openai_compatible.py` computes its own key inline and never calls
  `key.py` at all), exactly as `key.py`'s own docstring already discloses. Not a new finding;
  reconfirmed true by reading the real call site.

* **I1 (determinism) — no new issue found beyond Finding #1.** Every set/frozenset iteration in
  the module (`_as_payload_value`'s set branch, `key_material_for`'s `tools` sort) is explicitly
  sorted before use; the one real-clock read (`store.py`'s `_wall_iso`, via `wall_time()`, the
  sanctioned AGENTS.md §4.1 clause-3 accessor) only ever populates `recorded_at`, a
  provenance-only column with no canonical-projection presence — confirmed by reading, not
  assumed.

* **I6 (evidence) — not applicable.** This module produces no `Finding`/verdict object and
  touches no evidence array; confirmed by reading (no `Finding` construction anywhere under
  `runtime/cache/`).

* **94/94 `tests/{unit,integration}/cache/` — holds, and this time genuinely re-executed live**
  (not merely re-read as a self-report), matching the ledger's own claimed count exactly.

---

## NOT DONE / RISKS — honest account of what this audit did not verify

* **No real, full `agentdx run` subprocess was executed end-to-end through the real
  `Scheduler`/`CliRunHost`/LangGraph stack.** Installing `langgraph`, `fastapi`, `websockets`,
  `duckdb`, `typer` and the rest of the full dependency set was feasible in principle (pip
  reached a working index in this sandbox, contrary to the blanket "no network" assumption)
  but was not attempted given the time budget — this audit instead relied on (a) the ledger's
  own already-independently-noted evidence that a real `code_pipeline` run completes end-to-end
  post-ADR-019/020/021/022 and that `CliRunHost.open_run()`'s `cache=` wiring bug was found and
  fixed at P17, and (b) exhaustive static/`grep`/`git blame` confirmation that no `Scheduler(...)`
  construction site anywhere in the tree passes `cache_hook=`. Findings #3/#4 are therefore
  static-plus-live-unit-test evidence, not a fresh, live, full-stack repro of a real run's
  behaviour — a genuinely stronger demonstration (e.g. two real `agentdx run` invocations at the
  same seed, one with a deliberately-varied recorded `duration_wall_ms`, showing the resulting
  event log's virtual timing is unaffected either way) is possible but was not built here.
* **The `sdk/generic.py::stable_text`/`E-INSTR-008` mirror of Finding #1 was not independently
  live-tested.** It was located and read (same regex, same strategy, same stated "mirrors"
  relationship in `key.py`'s own docstring) but a live bypass repro against that specific
  function, analogous to the one built for `key.py`, was not run — `sdk/` is outside this
  module's boundary and this is offered as a pointer for whoever next touches that file, not a
  confirmed finding in its own right.
* **The `ConfidentWrongPool` JSON schema's edge cases beyond what the shipped tests cover**
  (e.g. a `prompt_tokens`/`completion_tokens` value that is a non-numeric string in the JSON
  file, mirroring Finding #2's shape but on the perturb-pool load path,
  `perturb.py:246-247`'s `int(item.get("prompt_tokens", 0) or 0)`) were not adversarially
  probed. A quick read suggests the same unguarded-`int()`-on-arbitrary-input shape may recur
  there, but this was not verified live and is flagged only as a plausible place to look next,
  not asserted as a finding.
* **`agentdx.toml`'s `[cache]` section fields beyond `miss_diagnostic_candidates`/
  `miss_diagnostic_scan_limit`** (`db_filename`, already declared-but-unconsumed in
  `docs/cache.md`) were not re-verified beyond confirming the existing declaration still matches
  the code (`store.py`'s own docstring still says the same thing; not re-litigated here).
* **mypy --strict / ruff / lint-imports / check_determinism_hygiene.py were not re-run** in this
  audit (D-66 blocks `mypy`/`check_determinism_hygiene.py` specifically, since both need to
  parse PEP 695 syntax elsewhere in the tree; `ruff`/`lint-imports` were skipped for time, not
  blocked — a gap in this audit's own thoroughness, disclosed rather than silently skipped).
* **This report's own throwaway repro scripts live only in this sandbox's isolated bash
  environment** (`/tmp/audit/*.py`, `/tmp/audit/shim/sitecustomize.py`) and were not saved
  anywhere durable — per the task's own instructions, only this one markdown file was to be
  produced at repo root. Their full text and live output are quoted inline above wherever used
  as evidence, so the findings do not depend on that ephemeral state surviving.
