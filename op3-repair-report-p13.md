# OP-3 repair report — `explore/` (P13, bounded schedule exploration)

Repairs `op2-audit-p13.md` (independent OP-2, fresh agent, cold read, second attempt after
the first spawn was cut off by a session rate limit; VERDICT **PASS WITH NOTES** — 0
CRITICAL, 1 HIGH (forward-looking, not a defect in shipped code), 1 MEDIUM, 4 LOW).
Performed by the orchestrating session directly (`Edit`/`Write`/`Bash`), same day as the
audit, under the standing "propose a full priority order and just go" authorization (no
CRITICAL finding, no systemic-trust implication, so no fresh `AskUserQuestion` round was
needed). Tenth and final stop in the "clearing verification debt" queue, directly following
the P16 repair cycle (`op3-repair-report-p16.md`).

## Scope decision

The audit's own headline conclusion: `explore/`'s shipped code has **no functional defect**
— the BFS/termination/dedup/reduction/report pipeline held against every hand-computed and
live-mutated test the audit threw at it, both claimed build-time bug fixes (`decision_step`'s
off-by-one, the `budget_exceeded`/`capped` split) genuinely hold against a real `Scheduler`,
and every static gate the audit could run (`ruff`, `ruff format`, `mypy --strict`,
`lint-imports`, `check_determinism_hygiene.py`, `check_bench_markers.py`) passed for real.

Findings #1, #3, #4, #5 were fixed in full. Finding #6 was fixed in full and, in doing so,
surfaced a second, previously-invisible real defect (a genuine transitive import-layer
violation), which was also fixed and verified. Finding #2 is **disclosed as a new deviation
(D-88) rather than built** — it is forward-looking (D-55's own scope boundary, not a defect
in P13's shipped code), and the audit's own live demonstration shows that wiring `explore()`
to the real fixtures today would be actively unsafe under D-80's cache-reuse behavior. Per
this project's standing "disclose rather than build something under time pressure that only
partially resolves an unspecified interaction" precedent (D-58, D-86, D-87), the correct
same-day action is to document the hazard precisely, not to design a fix for it.

## Finding #1 (MEDIUM) — `docs/exploration.md` has no "Error codes" section

**What the audit found:** `explore/`'s only error class (`MalformedRunError`, `E-EXPL-001`)
constructs a docs link that resolved to nothing — `AGENTS.md` §4's "carries an error code
plus a docs link" convention, and this project's own established per-module pattern, were
both violated.

**Fix:** a new `## Error codes` section was added to `docs/exploration.md`, before its
existing "Worked reference: reading a `Report`" section, with `<a id="e-expl-000"></a>`/
`### \`E-EXPL-000\`` and `<a id="e-expl-001"></a>`/`### \`E-EXPL-001\`` subsections. The
`E-EXPL-001` subsection explicitly cross-references the collision with `api/`'s (now-renamed)
`E-EXPLORE-001` — see Finding #3 below — so a future reader who lands on either anchor
understands both codes exist and why they no longer collide.

**Verified:** the anchors resolve to real headings in the same file; the cross-reference text
matches the actual renamed code confirmed present in `src/agentdx/api/routes/analysis.py`.

## Finding #2 (HIGH, forward-looking) — D-55's premise is stale; D-80 makes real wiring unsafe

**What the audit found:** D-55 (2026-08-19) states `explore()` is demonstrated only against a
synthetic Scheduler-driven harness because `sdk/langgraph.py` didn't route LangGraph's Pregel
dispatch through `Scheduler.yield_point`/`spawn` at P13's build time. That premise is now
**stale** — ADR-017/018/019 (closed after P13 shipped) wired exactly that routing, confirmed
live by the audit with a real, fresh `agentdx run fixtures/code_pipeline --cache-mode replay
--seed 123456` completing end to end in this sandbox.

But the audit also found, live, that the obvious next step — wiring `explore()` to the real
fixtures via the CLI's existing `CliRunHost` — is **actively unsafe today**: D-80's run-
identity/reuse optimization (ruled 2026-09-01, after P13 shipped) keys `run_id` on `(seed,
scenario_hash, graph_hash)` only, with no `delay_schedule` component. Driving `explore()`
through that path silently collapses every distinct schedule at one seed onto the same cached
run. The audit demonstrated this live: 2 of 3 `explore()`-driven executions at a fixed seed
were silently "reused" from cache, producing a fabricated report and exit 0, no warning.

**Why not built:** this is genuinely new information — D-80 postdates P13's build by two
weeks, and neither D-55 nor `explore/`'s own code could have anticipated a caching layer that
ignores `delay_schedule` when computing run identity. The audit itself frames this as needing
"a `CONTEXT.md` ledger correction at minimum... even if the underlying wiring work stays
deferred." Building a fix today (extending D-80's hash, adding a cache-bypass carve-out, or a
`--cache-mode` requirement) would be a real design decision affecting `runtime`/`cli`/
`explore` jointly — out of proportion for a same-day repair pass and exactly the kind of
under-specified cross-module decision this project's precedent says to disclose, not guess at
(same shape as D-86/D-87).

**Fix:** new deviation **D-88** (CONTEXT.md §9), appended after D-87 per the append-only
convention — refines D-55 without editing it, records the live-demonstrated D-80 interaction
hazard, and names three candidate resolutions for whoever picks this up next. `docs/
exploration.md`'s own "What is not built, and why" item 6 already forward-referenced "CONTEXT.
md §9 D-88" (written speculatively during this repair, before D-88 existed) — checked against
the actual next-free deviation number after D-87 and confirmed correct; no correction needed.

**Verified:** D-88 is the next unused ID after D-87 (confirmed by reading §9 in full); `docs/
exploration.md`'s forward-reference now points at a real row.

## Finding #3 (LOW) — `E-EXPL-001` collides with `api/routes/analysis.py`'s unrelated 409 code

**What the audit found:** `explore/`'s own `MalformedRunError` uses `E-EXPL-001` for real,
load-bearing reasons (`explore/`'s only error class). `api/routes/analysis.py`'s P14-scope
`GET /api/runs/{id}/exploration` 409 stub ("no bounded-exploration report is persisted for
this run yet") independently reused the identical string for an unrelated purpose. No
registry check exists project-wide to catch an error-code collision like this.

**Fix:** renamed the API route's code from `"E-EXPL-001"` to `"E-EXPLORE-001"` (a distinct
prefix) in `src/agentdx/api/routes/analysis.py`'s `NotYetAvailableError(...)` construction,
leaving `explore/`'s own `E-EXPL-001` untouched — `explore/` "owns" the `E-EXPL-NNN` prefix,
since it is the module with the real, structural code space (PRD §15's exploration errors),
while `api/`'s 409 stub is more naturally namespaced under its own route's vocabulary.
`tests/api/test_analysis.py`'s matching assertion (`test_get_exploration_always_409_in_this_
build`) updated to expect the new code.

**Verified:** `src/agentdx/api/routes/analysis.py` parses cleanly (`ast.parse`) and contains
exactly one occurrence of `"E-EXPLORE-001"` at the expected call site; `explore/`'s own
`MalformedRunError` usage of `"E-EXPL-001"` is unchanged (grepped, confirmed no other
occurrence exists project-wide). `tests/api/test_analysis.py` could **not** be executed live —
this hits the exact same standing D-66 blocker `op2-audit-p14.md`'s own repair disclosed:
`agentdx/api/models.py`'s `type JsonValue = str | int | ...` PEP 695 syntax is unparseable by
CPython 3.10's own parser (a `SyntaxError`, not an import error), and this sandbox is Python
3.10.12 project-wide. Verified via `ast.parse` and direct reading only, disclosed rather than
silently assumed passing — matching this project's I9 norm and every prior D-66-blocked
repair this session (P14, P17).

## Finding #4 (LOW) — stale test-count claim in `CONTEXT.md`'s P13 narrative

**What the audit found:** `CONTEXT.md`'s P13 narrative and §5 row 13 stated "56/56 `tests/
unit/explore/`... (60 new tests)"; real `pytest` collection at audit time showed 57 unit + 4
integration = 61.

**Fix:** the stale count corrected in-place (line-count-neutral edit) with a note citing this
audit and the date. §5 row 13 (updated fully as part of this repair cycle's own documentation
pass, see below) states the current real count directly rather than repeating the stale one.

**Verified:** `pytest tests/unit/explore/ tests/integration/explore/ -v` (see Finding #5's own
verification, same run) — 75 collected and passed, of which 61 are the original P13 suite (57
unit + 4 integration) and 14 are Finding #5's new direct boundary-value tests, below.

## Finding #5 (LOW) — no direct unit test exercises `_coerce_explore`'s own boundary validation

**What the audit found:** only indirect regression coverage existed for `[explore]`'s
coercion logic (`tests/unit/sdk/test_config.py`, `tests/unit/store/test_threshold_config.py`
— both testing that *other* config sections still resolve after `[explore]` was added, never
`_coerce_explore`'s own five checks directly). The audit live-verified the underlying logic is
fully correct (9/9 boundary probes — `delay_bound_k` at 6/-1, `schedule_cap_n` at 0/10001,
`time_budget_s` at 0.0/-5.0, `strategy="bogus_strategy"`, `upgrade_reduction_if_redundancy_
over` at 1.5/-0.1 — all correctly rejected) but flagged the coverage gap itself as real.

**Fix:** new `tests/unit/explore/test_config.py`, 14 tests, promoting the audit's own live
verification into the permanent suite: defaults match the PRD §15.1 table; each of the five
boundary checks (`delay_bound_k` 0-5, `schedule_cap_n` 1-10000, `time_budget_s` > 0, `strategy`
∈ {`delay_bounded`, `random`, `replay_set`}, `upgrade_reduction_if_redundancy_over` 0.0-1.0)
rejected just outside its bound and accepted at its exact inclusive edges. Every test passes
an explicit `config_path` pointing at a real, empty `agentdx.toml` (never `None`, never the
repo's own committed config) — this is deliberate: it's the exact discipline the audit's own
first internal attempt at this got wrong (a `None` config_path let the repo's real `agentdx.
toml` mask a broken argument-layer override), caught and corrected before being trusted.

**Verified — mutation-tested, not just passing:** confirmed 14/14 pass; then temporarily
changed `_coerce_explore`'s `delay_bound_k` upper bound from `5` to `6` in `src/agentdx/
config.py`, reran — 2 tests (the out-of-range and boundary-acceptance tests) turned red as
expected; restored the original bound, reconfirmed 14/14 green and `git diff --stat` clean
against `config.py`.

## Finding #6 (LOW, structural) — `.importlinter`'s `explore-below-transport` contract too loose

**What the audit found:** the contract only forbade `agentdx.api`/`agentdx.cli` — the
universal "nothing above the transport layer" rule every layer gets — rather than positively
restricting `explore/` to exactly `{runtime, analysis.race}` the way `CONTEXT.md` §4's table
implies and the way the sibling `scenario-is-declarative` contract already does for
`scenario/`. Harmless *at audit time* (the audit itself confirmed `explore/`'s real imports
were clean), but a future drift into `store/`/`sdk/` would go uncaught.

**Fix, part 1 — tighten the contract:** `.importlinter`'s `explore-below-transport` contract's
`forbidden_modules` extended from `[agentdx.api, agentdx.cli]` to `[agentdx.store, agentdx.sdk,
agentdx.scenario, agentdx.otel, agentdx.api, agentdx.cli]`, mirroring `scenario-is-
declarative`'s exhaustive pattern, with an explanatory comment citing this finding.
`CONTEXT.md` §4's architecture-map table (`explore/` row, "Must not import" cell) updated from
`—` to the same explicit list, with the same citation.

**Fix, part 2 — the tightening surfaced a real, previously-invisible violation:** running the
real `lint-imports` console script (`/sessions/.../.local/bin/lint-imports --config
.importlinter` — the direct binary path, not `python3 -m importlinter.cli`, which silently
exits 0 with **no output at all** in this sandbox, a red herring discovered and worked around
during this repair) against the tightened contract found **9 kept, 1 broken**:
`agentdx.explore.generate -> agentdx (l.18) -> agentdx.sdk.langgraph (l.98)`. Root cause:
`src/agentdx/explore/generate.py` line 18 read `from agentdx import wall_time` — importing the
bare top-level `agentdx` package rather than `wall_time`'s real defining module. `agentdx/
__init__.py` itself (grepped directly) re-exports `wall_time` from `agentdx.runtime.clock` at
line 59, but also imports `agentdx.sdk.langgraph` at line 98 — so any importer of bare
`agentdx`, including this one `from agentdx import wall_time` line, transitively pulls in
`sdk.langgraph` regardless of which name it actually asked for. This is a genuine `explore/`
→ `sdk` layering violation the loose contract had never been able to see, confirmed via `grep
-rn "^from agentdx import\|^import agentdx$" src/agentdx/explore` to be the only such pattern
anywhere in the module.

**Fix, part 2 (continued) — the actual repair:** `generate.py`'s `from agentdx import
wall_time` replaced with `from agentdx.runtime.clock import wall_time` (the real defining
module, confirmed by direct read) — `agentdx.runtime` is a layer `explore/` is explicitly
permitted to import (CONTEXT.md §4's table, `.importlinter`'s own contract, and `Budget`'s own
module docstring, which already states "`explore/` is free to import `runtime`"), so this is
a pure import-path correction with zero behavioral change — `wall_time()`'s two call sites
(`Budget.start`, `Budget.exceeded`) are untouched.

**Verified:**
- `lint-imports` re-run against the tightened contract: **10/10 contracts KEPT**, including
  `explore/ stays below the transport layer`, 154 files, 787 dependencies analyzed.
- `tests/unit/explore/ tests/integration/explore/` re-run in full after the import change:
  **75/75 passed**, 0.48s — nothing broke.
- `ruff check`/`ruff format --check` on `generate.py`: clean.
- `mypy --strict` on `src/agentdx/explore/` + `config.py` (via `--cache-dir=/tmp/...` — this
  sandbox's mounted `/mnt/outputs` filesystem returns a permission error on mypy's default
  `.mypy_cache/` write, unrelated to the code under test, same class of sandbox friction the
  audit itself hit and worked around with `--no-sqlite-cache`): **clean, 0 errors, 7 files**.

## Full verification (real execution throughout, via this session's disclosed stdlib-compat
shim — `PYTHONPATH=<shim>:src PYTHONHASHSEED=0`, backporting only `tomllib`/`datetime.UTC`/
`enum.StrEnum`, zero `agentdx` logic touched)

- `pytest tests/unit/explore/ tests/integration/explore/ -v` → **75 passed** (57 original unit
  + 14 new `test_config.py` + 4 integration), 0.48s.
- `ruff check` on every touched file (`generate.py`, `api/routes/analysis.py`, `test_config.py`,
  `test_analysis.py`, `docs/exploration.md`) → clean, all checks passed.
- `ruff format --check` on every touched Python file → clean, 3 files already formatted (one
  pre-existing, unrelated formatting note in `test_analysis.py` at a line this repair never
  touched — confirmed via `git diff`, left alone, out of this repair's scope).
- `mypy --strict --cache-dir=/tmp/...` on `src/agentdx/explore/` + `config.py` → clean, 0
  errors, 7 source files.
- `lint-imports --config .importlinter` (direct binary path) → **10/10 contracts KEPT**, 154
  files, 787 dependencies.
- `check_determinism_hygiene.py` → only the standing, pre-existing, unrelated D-66 failure
  (`api/models.py`'s PEP 695 syntax, confirmed project-wide by P14's own repair) — zero
  violations under `explore/` or any file this repair touched.
- `check_bench_markers.py` → clean, 17 published files scanned, every number measured.
- `check_ledger.py` → clean, §8/§9 append-only, 500-line cap respected, no dangling ADR refs.
- `tests/api/test_analysis.py`'s renamed-code assertion: verified via `ast.parse` + direct
  reading only — the standing D-66 PEP-695 blocker prevents live execution of anything under
  `tests/api/`, disclosed rather than silently assumed passing (same as P14's own repair).

No functional defect remains in `explore/`'s shipped code. **A second independent re-audit is
owed before `VERIFIED`**, same standing pattern as every module since P02.
