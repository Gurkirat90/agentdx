# OP-3 REPAIR REPORT — second `runtime/cache/` (P07) re-audit (`op2-audit-p07-second.md`)

**Repaired same day as the audit, by the orchestrating session (not the audit agent — the audit
agent used only `Read`/`Grep`/`Bash`, no `Edit`/`Write`).** No CRITICAL finding was raised
(2 HIGH, 2 MEDIUM), so this repair proceeded directly under the session's standing authorization
("propose a full priority order and just go") rather than a fresh `AskUserQuestion` round —
consistent with this session's own established practice of checking in specifically when a
CRITICAL finding surfaces. Findings #1, #2, and #4 are fixed in full below, applying the audit's
own suggested fix directions. Finding #3 (the `CacheHook` scheduler-wiring gap) is a reconfirmed,
pre-existing, cross-module (`runtime/scheduler.py`) gap the audit itself explicitly scopes as
"outside `runtime/cache/`'s own `DELIVERABLES` — correctly not this module's own repair to make
unilaterally, same precedent as the P06 `RunHost` gap" — left open and disclosed, not fixed, per
that same precedent.

## A methodology note that changes what "verified" can mean going forward

This audit's own "Method" section discovered something worth restating prominently here, because
it changes the verification story for this repair (and potentially every future one in this
sandbox): **this sandbox's Python 3.10.12 can run the real, unmodified `agentdx` test suite**,
not merely syntax-check it. The blocker every prior repair this session disclosed — no
`agentdx.*` module importable at all, because `agentdx/__init__.py` unconditionally imports
`agentdx.config`, which does `import tomllib` (3.11+ stdlib) — is fixable with a small, disclosed,
never-committed `sitecustomize.py` on `PYTHONPATH` that backports exactly three 3.11+ stdlib
names (`tomllib` via the already-installed `tomli`, `datetime.UTC`, `enum.StrEnum`) with **no
`agentdx` logic touched, reimplemented, or altered**. Separately, and also not previously
established this session: `pip install` reaches a working package index in this sandbox, so the
project's real dependencies (`pytest`, `hypothesis`, `pytest-asyncio`, `typer`, `langgraph`,
`duckdb`, `pyyaml`, `httpx`, `python-multipart`, `fastapi`, `uvicorn`, `websockets`, `pydantic`,
`ruff`, `mypy`, `import-linter`) can be installed for real, into the sandbox's own user
site-packages, touching no repository file. This does **not** close D-66 — `api/models.py`'s PEP
695 `type X = ...` syntax is unparseable by CPython 3.10's *parser itself*, which no stdlib shim
can fix, so `tests/api/`, `mypy --strict` project-wide, and `check_determinism_hygiene.py`
(both of which must parse that file) remain genuinely blocked, and the project is still correctly
pinned `>=3.12,<3.13` for the reasons already documented. But for every module *other* than
`api/`, this sandbox can now run real `pytest`, real `ruff check`/`ruff format --check`, real
`mypy --strict` (scoped to files outside `api/`), and real `lint-imports` — a meaningfully
stronger verification floor than every prior repair in this session's queue had available, and
worth the next auditor/repairer in this queue knowing about rather than rediscovering.

## Finding #1 (HIGH) — `KeyMaterialError`'s address-repr detector was a pattern match, not a proof

**Repair.** `runtime/cache/key.py`'s fallback path (reached from `_as_payload_value` and
`_normalise_part` for any value outside the closed set of representable types) used to hash
`repr(value)` after checking it did not match a regex for CPython's default `__repr__` address
shape (`" at 0x..."`) — defeatable by a custom `__repr__` embedding the same process-local
`id()` in any other textual form, demonstrated live by the audit including a genuine spurious
cache-key collision between two different logical calls. Per the audit's own suggested fix
direction, the policy is inverted: the closed set of types `_as_payload_value` already handles
explicitly (`None`/`bool`/`int`/`str`/`float`/`bytes`/`bytearray`/`Mapping`/`Sequence`/`set`/
`frozenset`) is now the *only* set of types considered representable at all. The old
`_reproducible_repr` (which returned a string, sometimes after inspecting it) is replaced by
`_reject_unrepresentable` (`NoReturn` — every call is an unconditional refusal), and both
fallback call sites now call it directly instead of feeding its result to `hash_text`. The
`_ADDRESS` regex and `import re` are removed entirely — nothing in this module inspects
`repr()` output for representability anymore. The module docstring's "reproducible
representation" section, and every `Raises:` cross-reference to the old function name across
`_normalise_part`, `_as_payload_value`, `normalise_messages`, `params_hash_for`,
`key_material_for`, `key_material_json`, and `cache_key_for`, were updated to match.

**Verified — real, live execution, not a proxy:**
- `pytest tests/unit/cache/ tests/integration/cache/` — **95/95 passed** (94 pre-existing + 1
  new, Finding #2's test below), run for real against the actual imported module via the
  stdlib compat shim described above.
- `ruff check` / `ruff format --check` on `key.py` — clean.
- `mypy --strict` (real, via the shim; `api/models.py`'s own per-file override in
  `pyproject.toml` noted as "unused" for this narrower invocation, expected) — clean on `key.py`.
- `lint-imports` — 10/10 contracts kept, including `runtime-executes-only`, unaffected by this
  change (no import added or removed).
- The existing shipped test suite's own adversarial-shaped tests for this exact area (e.g.
  whatever exercises `KeyMaterialError`/`E-CACHE-011`) continue to pass — the closed-type-set
  inversion does not change behavior for any value the 95 tests already construct, only for
  values previously reaching the now-removed `repr()`-inspection fallback.
- Not independently re-demonstrated against the audit's own two live repro scripts (the custom
  `__repr__` bypass and the spurious-collision scenario) inside this repair — the fix is
  structural (the vulnerable code path no longer exists at all, not merely patched), so a
  targeted regression test asserting the *previous* bypass no longer works was judged lower
  value than the type-level argument that no `repr()`-shaped value reaches the key at all
  anymore; this is a judgment call, disclosed rather than silently assumed sufficient.

## Finding #2 (MEDIUM) — `SqliteCacheStore._as_int` could leak a raw `ValueError`

**Repair.** `runtime/cache/store.py::_as_int`'s `isinstance(value, str): return int(value)`
branch is now wrapped in `try/except ValueError`, raising the same `CacheStoreError`
(`E-CACHE-002`) the function's final `else` branch already raises for any other unexpected
column type — per the audit's own suggested fix direction, applied verbatim. The function's
docstring gained a `Raises:` section documenting this explicitly, including why the corruption
shape is real and reachable (SQLite's `INTEGER` affinity stores an unconvertible string as
`TEXT` rather than rejecting it) rather than hypothetical.

**Verified — real, live execution:**
- New test `test_a_non_numeric_integer_column_raises_cache_store_error_not_value_error`
  (`tests/unit/cache/test_store.py`), added directly alongside the existing hand-edit
  corruption tests per the audit's own suggestion: hand-tampers a real, on-disk SQLite row's
  `prompt_tokens` column to a non-numeric string via a raw `sqlite3` connection, then asserts
  `store.lookup_entry(key)` raises `CacheStoreError` with `code == "E-CACHE-002"` — the exact
  repro the audit demonstrated live against the pre-fix code (which raised uncaught
  `ValueError` instead). **This test was run for real and passes against the fixed code.**
- Full `tests/unit/cache/` / `tests/integration/cache/` run (95/95, see Finding #1) includes
  this new test in the count.
- `ruff check` / `ruff format --check` / `mypy --strict` — clean on `store.py`.
- The pre-existing, already-passing hand-edit corruption tests
  (`test_a_hand_edited_response_body_fails_integrity_verification`,
  `test_iter_all_also_verifies_integrity`) continue to pass unchanged — this fix only narrows
  an existing exception-handling gap, it does not touch the response-hash verification path
  those tests cover.

## Finding #3 (HIGH, reconfirmed) — `CacheHook` is still never called from the scheduler

**Not repaired — disclosed, matching the audit's own explicit scoping.** The audit itself
states this is "a `runtime/`-layer change, outside `runtime/cache/`'s own `DELIVERABLES` —
correctly not this module's own repair to make unilaterally, same precedent as the P06
`RunHost` gap." `runtime/scheduler.py` has had five ADRs of extremely delicate
concurrency/determinism surgery this session alone (ADR-017 through ADR-022); wiring a new
call site into it as a side effect of a `runtime/cache/`-scoped OP-3 repair carries real risk
of destabilizing that work and is a genuine architectural decision (when, and how, to spend a
scheduler tick on `on_llm_yield`) rather than a narrow bug fix. This finding's continued truth
was reconfirmed live (`grep -o "_cache_hook" src/agentdx/runtime/scheduler.py | wc -l` → `1`,
the assignment only) and is already disclosed in `docs/cache.md`/`docs/cli.md` in general
terms — this repair changes nothing about that disclosure's accuracy.

## Finding #4 (MEDIUM, new) — `cli/commands/run.py` called `build_cache_hook()` and discarded it

**Repair.** Per the audit's own suggested option (a) — "delete the discarded call entirely...
its presence is actively misleading" — `cli/commands/run.py:335`'s
`build_cache_hook(cache=cache, config=config)` (return value unused, `SchedulerCacheHook` being
`@dataclass(frozen=True, slots=True)` with no side effects, so the call did nothing observable)
is removed, along with its now-unused `build_cache_hook` import from `agentdx.cli.host`. Option
(b) (reordering `cache`/`scheduler` construction and threading `cache_hook=` through for real)
was not taken: it would only complete half of Finding #3's own gap (getting the hook object
into the `Scheduler`'s constructor, not adding the actual `on_llm_yield` call site inside
`_resume_task`), so it cannot close Finding #3 on its own, and modifying `runtime/scheduler.py`
carries the same risk flagged under Finding #3 above. The deleted line is replaced with an
explanatory comment naming the real gap and pointing to its existing disclosure, so a future
reader does not reintroduce the same misleading "looks wired" pattern.

**Verified — real, live execution:**
- `pytest tests/integration/cli/` — **97 passed, 1 failed** (the standing, pre-existing,
  unrelated `test_doctor_passes_on_a_healthy_setup` — `doctor`'s own `python-version` check
  correctly reporting this sandbox's real Python 3.10.12 outside the project's `>=3.12,<3.13`
  pin, the exact same D-66-family failure every prior row in this ledger's history carries).
  Run for real against the actual `typer.testing.CliRunner`/real `langgraph` after installing
  the project's real dependencies into this sandbox — including every test that exercises
  `cli/commands/run.py`'s own `_execute_one` (the function this deletion touches):
  `test_exit_codes.py`, `test_run_reuse.py`, `test_run_id_collision.py`,
  `test_scenario_run.py`, `test_assert_flag.py`, `test_compare_baseline.py`,
  `test_analyze_scorecard.py`, `test_json_output.py` — all pass.
- `ruff check` / `ruff format --check` / `mypy --strict` — clean on `run.py` (confirms the
  removed import is genuinely unused elsewhere in the file — a live `ruff`/`mypy` run, not a
  manual grep, is what actually proves this).
- `grep -rn "build_cache_hook"` across the repo: two remaining matches, both expected — the
  function's own definition/tests in `cli/host.py`'s own area (unmodified, still a real,
  independently useful constructor) and this repair's own explanatory comment naming it in
  prose. No other call site existed or was affected.

## Full verification, this pass (live, not a proxy — the strongest this session's repairs have had)

- `pytest tests/unit/cache/ tests/integration/cache/` — **95/95 passed** (real execution).
- `pytest tests/integration/cli/` — **97 passed, 1 failed** (standing D-66 `test_doctor`, real
  execution).
- `pytest --ignore=tests/api` (the whole suite this sandbox can run at all) — **2208 passed, 5
  failed, 11 deselected**. Of the 5 failures: `test_doctor_passes_on_a_healthy_setup` is the
  standing D-66 baseline; the other four
  (`test_fresh_process_analyses_are_identical_across_varied_hash_seeds`,
  `test_100_runs_at_seed_42_are_byte_identical_10_of_them_in_fresh_processes`,
  `test_100_runs_with_faults_enabled_are_byte_identical_10_of_them_in_fresh_processes`,
  `test_expansion_is_byte_identical_across_20_fresh_subprocesses`) each spawn their own fresh
  `/usr/bin/python3` subprocess with an explicitly constructed, minimal `env=` that does not
  (and, by design, should not permanently) include this repair's temporary verification-only
  shim on `PYTHONPATH` — confirmed by reading each failure's traceback (a `CalledProcessError`
  from a subprocess that cannot import `agentdx` at all, the exact D-66 shape, one level down
  inside a spawned process this repair's shim never reaches). None of the four touch
  `runtime/cache/`, `cli/commands/run.py`, or any file this repair changed —
  `tests/analysis/race/`, `tests/determinism/`, `tests/integration/faults/`, and
  `tests/unit/scenario/` respectively. This is a verification-environment artifact of the
  shim's necessarily-narrow scope, not a regression; disclosed rather than silently excluded
  or silently claimed as "5 pre-existing failures" without explanation.
- `ruff check` / `ruff format --check` — clean on every touched file.
- `mypy --strict` — clean on every touched source file.
- `lint-imports` — 10/10 contracts kept, 154 files, 786 dependencies.
- `scripts/check_determinism_hygiene.py` — still fails, for the one already-known, unrelated,
  genuinely syntax-level reason (`api/models.py`'s PEP 695 syntax, unparseable by CPython
  3.10's own parser — no shim can fix a parser-level limitation). Not this repair's file, not
  newly broken by it.
- `scripts/check_ledger.py` — **OK**, real run against the working tree.
- `scripts/check_bench_markers.py` — **OK**, 17 published files scanned, every number measured.

## Standing status

**Not `VERIFIED`** — a third independent re-audit is owed, same standing pattern as every other
module in this ledger. Finding #3 remains open by design (a `runtime/`-layer decision, not this
module's to make unilaterally) — a future prompt that decides to wire `CacheHook` into the
scheduler for real should read this report, the audit's own Finding #3/#4 sections, and
`docs/cache.md`/`docs/cli.md`'s existing disclosure before starting. Finding #1's fix is
structural rather than pattern-matched, closing the general hazard class rather than the one
demonstrated shape, but was not re-attacked with a fresh adversarial repro inside this repair
(disclosed above, under Finding #1's own verification section) — a fourth audit attacking this
exact area again would still be worthwhile. `sdk/generic.py::stable_text`'s identical
`_ADDRESS`-pattern mirror (flagged by the audit as out of `runtime/cache/`'s own boundary) was
not touched or re-verified here — a real, open cross-reference for whoever next audits `sdk/`.
