# OP-2 INDEPENDENT AUDIT — P13 `explore/` (bounded schedule exploration, PRD §15)

**Scope.** `src/agentdx/explore/{schedule,generate,reduce,dedup,report}.py` + `__init__.py`;
`tests/unit/explore/*` (incl. `_factories.py`); `tests/integration/explore/test_dod.py` +
`_harness.py`; `docs/exploration.md`; `src/agentdx/config.py`'s `ExploreConfig`/`_coerce_explore`;
`agentdx.toml`'s `[explore]` section; the `.importlinter` `explore-below-transport` contract.
Cross-checked against, but not itself re-audited: `runtime/scheduler.py` (only the two surfaces
`explore/` depends on — `Scheduler._choose`'s step-then-increment order, `delay_schedule=`
construction — were read, not the whole module), `analysis/race.py` (reused verbatim, treated as
a black box per P12's own `VERIFIED` status), `sdk/langgraph.py` (read only to verify the D-55
claim itself, per the assignment's instruction — see Finding #2).

**Method.** Read `CONTEXT.md` in full (§0–§14, all of §5/§6/§7/§9/§10/§11/§13 relevant rows), all
of `AGENTS.md`, PRD §15.1–§15.6 in full plus the worked repro example immediately above §15. Read
every line of all six `explore/` source files and `docs/exploration.md`, and every test in
`tests/unit/explore/` and `tests/integration/explore/test_dod.py`. Cross-checked `config.py`'s
`ExploreConfig`/`_coerce_explore` against `agentdx.toml`'s `[explore]` section and PRD §15.1's
table, line for line. Read the `.importlinter` `explore-below-transport` contract's actual text
against CONTEXT.md §4's architecture-map table. Grepped for every `agentdx.explore` import
project-wide to check for undisclosed consumers. Read `CONTEXT.md`'s ADR-017/ADR-018/ADR-019/D-62/
D-80/D-84 history and the current `sdk/langgraph.py`/`tests/integration/runtime/
test_d62_fanout_dispatch.py` to check whether D-55's disclosed premise is still accurate today
(explicitly asked for by the assignment).

**Real execution achieved — full account.** This sandbox is Python 3.10.12; the project is pinned
`>=3.12,<3.13`, and `agentdx/__init__.py` unconditionally imports `agentdx.config`, which does
`import tomllib` (3.11+ stdlib). Built a disclosed, throwaway, never-committed compatibility shim
at `/sessions/.../mnt/outputs/audit-shim/sitecustomize.py` (outside this repo, never on a path
that would be committed) backporting exactly three symbols — `tomllib` (aliased to the
already-installed third-party `tomli` package), `datetime.UTC`, `enum.StrEnum` — with **zero**
`agentdx` logic reimplemented or patched. Confirmed all of `langgraph`, `fastapi`, `uvicorn`,
`websockets`, `pydantic`, `python-multipart`, `typer`, `duckdb`, `pyyaml`, `httpx`, `ruff`, `mypy`,
`pytest`, `pytest-asyncio`, `hypothesis` already importable in this sandbox — no installs needed.
With `PYTHONPATH=<shim>:src` and `PYTHONHASHSEED=0`, this let **real, unmodified project code and
the real test suite run**, not simulated:

- `pytest tests/unit/explore/ tests/integration/explore/ -v` → **61 passed** (57 unit + 4
  integration), 0.47s, real interpreter, real imports, real `runtime.scheduler.Scheduler` driving
  the two integration harnesses.
- `ruff check` / `ruff format --check` on `explore/` + its tests + `config.py` → clean (17 files
  already formatted, all checks passed), ruff 0.16.6.
- `mypy --strict` on `src/agentdx/explore/` + `config.py` → **clean, 0 errors, 7 source files**
  (mypy 2.3.1; needed `--no-sqlite-cache` — the sandbox's mounted `/mnt/outputs` filesystem
  returns `disk I/O error` for mypy's default sqlite-backed cache, unrelated to the code under
  test). A full `mypy --strict src/agentdx/` was also attempted for corroboration but is blocked
  tree-wide by two **pre-existing, already-disclosed, unrelated** gaps: `api/models.py`'s PEP 695
  syntax is unparseable by CPython 3.10's own `ast` module (D-66, confirmed project-wide, not new)
  and `scenario/loader.py` needs `types-PyYAML`, not installed in this sandbox. Neither touches
  `explore/`.
- `lint-imports` (via `python3 -m importlinter.cli`) → real run, **10/10 contracts kept**, 154
  files, 787 dependencies, including `explore/ stays below the transport layer`.
- `scripts/check_determinism_hygiene.py` → real run; the only violation reported is the same
  pre-existing `api/models.py` PEP-695 parse failure (D-66); **zero violations under `explore/`**.
- `scripts/check_bench_markers.py` → real run, **clean**, 17 published files scanned including
  `docs/exploration.md`.
- Live mutation tests (four, all against the real, unmodified source, monkeypatched only in a
  throwaway in-process script — see Findings/Positive-controls below for each): `Report.
  __post_init__`'s I10 guard, `schedule.signature`'s dedup hashing, `Turn.decision_step`'s
  off-by-one fix, and the `budget_exceeded`/`capped` split. All four broke exactly the tests their
  own docstrings claim protect them, and nothing else.
- Live boundary-value tests against `_coerce_explore` (9 probes: `delay_bound_k` at 6 and −1,
  `schedule_cap_n` at 0 and 10001, `time_budget_s` at 0.0 and −5.0, `strategy="bogus_strategy"`,
  `upgrade_reduction_if_redundancy_over` at 1.5 and −0.1) — all 9 correctly rejected with a clear
  `ConfigError` citing the exact PRD bound and the bad value (see Finding #4 for why the *first*
  attempt at this looked like a defect and was not one).
- **A real, fresh, non-reused `agentdx run fixtures/code_pipeline --cache-mode replay --seed
  123456` completed end-to-end in this sandbox, exit 0** — driven through the real CLI
  (`typer.testing.CliRunner` against the real `agentdx.cli.main.app`), the real `Scheduler`, and
  the real `sdk.langgraph.LangGraphAdapter`, using a pre-existing `~/.agentdx/cache.db` this
  session did not create. This is the single most consequential live result in this audit — see
  Finding #2.
- A broader regression pass (`tests/unit/`, `tests/integration/explore/`, `tests/analysis/`,
  `tests/false_positives/`, `tests/determinism/`, excluding `tests/unit/api` which needs PEP 695):
  **≈2056 passed, 3 failed**, all three of them the standing, already-documented D-66-class
  fresh-subprocess failures (`test_matrix.py::test_expansion_is_byte_identical_across_20_fresh_
  subprocesses`, `race/test_determinism.py::test_fresh_process_analyses_are_identical_across_
  varied_hash_seeds`, `test_replay_equality.py::test_100_runs_at_seed_42_are_byte_identical_10_
  of_them_in_fresh_processes` — each spawns a **fresh** `python3` subprocess whose own explicit
  `env=` does not inherit `PYTHONPATH`, so the shim never loads there). Nothing new broken by, or
  in, `explore/`.

No functional gap remains between "static reading" and "real execution" for this module — every
claim in `CONTEXT.md`'s P13 narrative that a command was run was independently re-run here, for
real.

---

## VERDICT: **PASS WITH NOTES**

`explore/`'s own shipped code, as delivered, has **no functional defect** found in this audit: the
BFS/termination/dedup/reduction/report pipeline is correct against every hand-computed and
live-mutated test thrown at it, both claimed build-time bug fixes (the `decision_step` off-by-one,
the `budget_exceeded`/`capped` conflation) genuinely hold against a real `Scheduler`, the I10
honesty mechanism is real (type-level, not convention), and every static gate (`ruff`, `ruff
format`, `mypy --strict`, `lint-imports`, `check_determinism_hygiene.py`, `check_bench_markers.py`)
passes for real. D-54 and D-55 are accurately declared as of P13's own build date. But this audit
found:

1. **(MEDIUM)** `docs/exploration.md` has no "Error codes" section — `explore/`'s only error class
   (`MalformedRunError`, `E-EXPL-001`) constructs a docs link that resolves to nothing, violating
   `AGENTS.md` §4's "carries an error code plus a docs link" and this project's own established
   per-module convention.
2. **(HIGH, forward-looking — not a defect in shipped `explore/` code)** D-55's stated premise
   ("`sdk/langgraph.py` doesn't route LangGraph dispatch through `Scheduler.yield_point`/`spawn`
   yet") is now **stale**: ADR-017/018/019 closed that gap after P13 shipped, confirmed live in
   this sandbox by a real `agentdx run fixtures/code_pipeline` completing end to end. But the
   obvious next step — wiring `explore()` to the real fixtures via the CLI's existing `CliRunHost`
   machinery — is live-demonstrated here to be **actively unsafe**: D-80's run-identity/reuse
   optimization (ruled 2026-09-01, after P13) keys `run_id` on `(seed, scenario_hash, graph_hash)`
   only, with no `delay_schedule` component, so driving `explore()` through that path silently
   collapses every distinct schedule at one seed onto the same cached run — demonstrated live, 2 of
   3 executions silently "reused," a fabricated report, exit 0, no warning. This is genuinely new
   information (D-80 postdates P13's build) that no `CONTEXT.md` row currently discloses.
3. **(LOW)** `E-EXPL-001` is also used, unrelatedly, by `api/routes/analysis.py`'s 409
   "exploration not yet available" stub (P14-scope code, not P13's own) — a real code-identifier
   collision with no registry check to catch it.
4. **(LOW)** `CONTEXT.md`'s P13 narrative and §5 row 13 state "56/56 `tests/unit/explore/`... (60
   new tests)"; the actual count, confirmed by real `pytest` collection, is **57 unit + 4
   integration = 61**.
5. **(LOW)** No direct unit test exercises `_coerce_explore`'s own boundary-value validation
   (only indirect regression coverage that *other* config sections still resolve). Live-verified
   here that the logic itself is fully correct (9/9 boundary probes correctly rejected) — this is
   a test-coverage gap, not a functional defect.
6. **(LOW, structural note)** The `.importlinter` `explore-below-transport` contract only forbids
   `agentdx.api`/`agentdx.cli`; it does not positively restrict `explore/` to exactly
   `{runtime, analysis.race}` the way `CONTEXT.md` §4's table implies, or the way the
   `scenario-is-declarative` contract does for `scenario/`. Harmless today (actual imports are
   clean) but a future prompt importing e.g. `store/`/`sdk/` into `explore/` would not be caught.

None of these rises to a false claim, a scope violation, an I-numbered invariant breach, or a test
with no real discriminating power — the pattern this project's history (P11-second, P08-third)
most worries about. That is why this is **PASS WITH NOTES**, not **FAIL**: every item is real,
demonstrated, and actionable, but none invalidates the module's own claimed correctness.

---

## FINDING #1 (MEDIUM) — `docs/exploration.md` has no Error-codes section; `E-EXPL-001`'s docs link is dead

**Where:** `src/agentdx/explore/schedule.py:37–66` (`ExploreError`/`MalformedRunError`);
`docs/exploration.md` (entire file — no matching section).

Every other audited module in this codebase documents its error codes in a dedicated section of
its own doc file, which is what makes the `f"({_DOCS}#{code.lower()})"` construction in
`ExploreError.__init__` actually resolve to something:

```
docs/cache.md:345:## 11. Error codes
docs/cache.md:348:### `E-CACHE-001` — cache miss in replay/perturb
docs/chaos-safety.md:107:### E-CHAOS-001
```

`docs/exploration.md` has no such section at all — confirmed by listing every heading in the file:

```
$ grep -n "^#" docs/exploration.md
1:# Bounded schedule exploration (`agentdx.explore`)
8:## The one rule everything else follows from
19:## What this module does
48:## Why finding-detection is unchanged: no new detector
56:## Independence-based reduction, and its honest limitation (PRD §15.4, Design Constraint 3)
96:## A second bug caught and fixed during this build
126:## Why the demonstration below is a synthetic harness...
168:## Definition of done, demonstrated
223:## What is not built, and why
245:## Worked reference: reading a `Report`
```

Live demonstration of the resulting dead link:

```
$ python3 -c "
from agentdx.explore.schedule import MalformedRunError
try:
    raise MalformedRunError('two schedule_decision events disagree')
except MalformedRunError as e:
    print(str(e))
"
[E-EXPL-001] two schedule_decision events disagree (docs/exploration.md#e-expl-001)
```

That anchor exists nowhere in the file. `AGENTS.md` §4 states "Errors are typed and carry an error
code (`E-XXX-NNN`) plus a docs link" — the code is real and typed, but the link is dead. The one
test that raises this error (`tests/unit/explore/test_schedule.py::
test_turns_from_events_raises_on_conflicting_chosen_task_id`) only asserts
`pytest.raises(MalformedRunError)`, never the code or the message text, so nothing in the suite
would have caught this.

**Suggested fix direction:** add a short "## Error codes" section to `docs/exploration.md` with a
`### E-EXPL-001` (and, for completeness, `### E-EXPL-000` for the never-directly-raised base
class) heading, matching `docs/cache.md`'s established format.

---

## FINDING #2 (HIGH, forward-looking — not a defect in `explore/`'s shipped code) — D-55's premise is stale, and the obvious next step is live-demonstrated to be unsafe

**Where:** `CONTEXT.md` §9 row D-55 (text unchanged since 2026-08-19); `src/agentdx/sdk/
langgraph.py:696` (`run.scheduler.spawn(...)`); `src/agentdx/cli/host.py:284–319`
(`CliRunHost.open_run`'s D-80 reuse check); `tests/integration/runtime/
test_d62_fanout_dispatch.py` (already-committed evidence the dispatch gap closed).

The assignment asked specifically to verify D-55's own claim — "does the code really only touch
the synthetic harness? are there any code paths that silently assume the literal fixtures work?"
— rather than re-litigate the scope decision itself. Two things follow from checking that claim
against the *current* tree (2026-09-07), not the tree as it stood when D-55 was written
(2026-08-19):

**Part A — the premise is stale.** D-55's text reads: *"Neither fixture's execution path routes
through `Scheduler` at all: both run through `sdk/langgraph.py`'s `LangGraphAdapter`, which
records LangGraph node reads/writes/spans but never routes LangGraph's own Pregel dispatch through
`Scheduler.yield_point`/`spawn`."* That was true on 2026-08-19. It is not true today:

```
$ grep -n "scheduler.spawn\|yield_point" src/agentdx/sdk/langgraph.py
696:            task_id = run.scheduler.spawn(
746:            await run.scheduler.yield_point("sdk_node_entry")
```

`CONTEXT.md`'s own ADR-017 (2026-09-02) wired exactly this; ADR-018/019 (2026-09-03, documented
live in `test_d62_fanout_dispatch.py`'s own docstring: *"the dispatch gap this file documented is
closed, by candidate β"*) then closed the follow-on fan-out-deadlock bug. Confirmed live, in this
sandbox, not just by citation — a fresh, non-reused run of the literal fixture completes:

```
$ python3 -c "
import sys; sys.argv = ['agentdx','run','fixtures/code_pipeline','--cache-mode','replay','--seed','123456']
from agentdx.cli.main import main
main()"
code_pipeline: passed
  run_id: r_41642448
  verdict: state_conflict_risk (confidence low)
Bounded search: absence of findings is not proof of absence.
SystemExit code: 0
```

`CONTEXT.md`'s own ADR-017 row already anticipates this precisely — *"D-55 ... is not closed by
this decision — Option A would have; this trade is accepted, not overlooked"* — so the project is
not unaware the underlying blocker moved. But D-55's own §9 row has not received the
`AGENTS.md` §10 correction-append treatment (append a corrected row, mark the original "Reconciled?
→ superseded by D-nn"); a reader who jumps straight to §9's D-55 row (as §7's P13 paragraph and §5
row 13 both point to) still reads the 2026-08-19 premise as current fact.

**Part B — the obvious next step is unsafe, and this is genuinely new information.** Given (A), it
is natural to ask whether `explore()` could now be driven against the literal fixture by reusing
the CLI's existing real-run machinery (`cli.commands.run._execute_one`/`cli.host.CliRunHost`)
instead of hand-building a fresh `Scheduler` per call the way `_harness.py` does. I tried this
live: a throwaway script (`typer.testing.CliRunner`, an injected `Scheduler` subclass forcing
`delay_schedule=` into `_execute_one`'s construction) driving `explore()` with `delay_bound_k=1,
schedule_cap_n=5` against `fixtures/code_pipeline` at one fixed seed:

```
  delay_schedule='[]'       run_id=r_2f0b7eb6 reused=False elapsed=0.340s
  delay_schedule='[[5,1]]'  run_id=r_2f0b7eb6 reused=True  elapsed=0.018s
  delay_schedule='[[6,1]]'  run_id=r_2f0b7eb6 reused=True  elapsed=0.018s
```

All three distinct delay schedules resolved to the **same** `run_id`, and the CLI's own D-80
reuse optimization (`RunAlreadyExistsError` path, `cli/host.py:284–319` — ruled 2026-09-01, after
P13 shipped) silently short-circuited schedules 2 and 3 back to schedule 1's already-sealed run.
`explore()` itself behaved exactly as designed — it has no way to know its injected
`ScheduleExecutor` is lying about determinism (the `ScheduleExecutor` protocol's own contract:
"the same `delay_schedule` must return the same log ... on every call" was technically satisfied,
just against the *wrong* delay_schedule for two of the three calls) — and produced a fully
plausible, entirely fabricated report:

```
Bounded schedule exploration
  delay bound (k)        1
  schedules executed     3  (cap 5)
  reduced away           2   (provably equivalent under independence)
  new findings           1     (write_write draft.module_a, first seen at k=0)
```

That "3 schedules executed" and "2 reduced away" are not real — the 2nd and 3rd rows are the same
cached events as the 1st, relabeled. `docs/exploration.md`'s "What is not built, and why" §4
("No CLI or API surface beyond the reporting payload itself") predates D-80 and could not have
anticipated this specific hazard. This is not a defect in `explore/`'s own shipped code — nothing
here contradicts D-55, and `explore/` never attempts this integration itself — but it is a real,
live-demonstrated, currently undocumented landmine for whoever closes D-55 next: the reuse check
must be bypassed or widened to include `delay_schedule` (or exploration must build its own
`Store`/`Scheduler` per call, as `_harness.py` already does, rather than going through
`CliRunHost.open_run`), or a future "real-fixture exploration" feature will silently lie.

**Suggested fix direction:** not this audit's to fix (read-only), but concretely: either (a) append
a corrected D-55 row noting the SDK-layer premise closed and pointing at this new D-80 interaction
as the actual remaining blocker, or (b) fold this into a fresh finding/ADR the next `sdk/langgraph.py`-
or `explore/`-touching prompt must read before attempting literal-fixture wiring.

---

## FINDING #3 (LOW) — `E-EXPL-001` is a cross-module code collision

**Where:** `src/agentdx/explore/schedule.py:65–66` (`MalformedRunError`, fixed to `E-EXPL-001`);
`src/agentdx/api/routes/analysis.py:294–300` (`NotYetAvailableError("E-EXPL-001", ...)`).

Two semantically unrelated conditions share the identifier `E-EXPL-001`: `explore/`'s own
"a run's event log has no usable `schedule_decision` structure" (raised inside `turns_from_events`)
and `api/`'s "no bounded-exploration report is persisted for this run yet" (a 409 the
`GET /api/runs/{id}/exploration` stub always returns — itself an honestly-disclosed P14 gap, not
a defect). Both are real, both are tested (`tests/unit/explore/test_schedule.py` for the first;
`tests/api/test_analysis.py:205` — `assert body["error"]["code"] == "E-EXPL-001"` — for the
second), so this was not an accident in either individual module, just an uncoordinated one across
two prompts three weeks apart (P13 on 2026-08-19; the API stub is not dated earlier than P14,
2026-08-24). No CI check enforces error-code uniqueness project-wide (`scripts/` has no such
script; `justfile`'s `ci` target has no equivalent to `check_bench_markers.py` for this class of
convention). Low severity because neither call site is currently reachable from the other's
context and no test depends on the codes being distinct, but a grep-based lookup of "E-EXPL-001"
in a future incident would surface two unrelated answers.

---

## FINDING #4 (LOW) — Test-count drift in `CONTEXT.md`

**Where:** `CONTEXT.md` §5 row 13 and §7's P13 paragraph both state "56/56 `tests/unit/explore/`
... (60 new tests)".

Real count, confirmed twice (pytest collection and a direct `grep -c "def test_"`):

```
$ grep -c "def test_" tests/unit/explore/*.py tests/integration/explore/*.py
tests/unit/explore/test_dedup.py:5
tests/unit/explore/test_generate.py:10
tests/unit/explore/test_reduce.py:19
tests/unit/explore/test_report.py:11
tests/unit/explore/test_schedule.py:12      # unit total: 57, not 56
tests/integration/explore/test_dod.py:4
```

`pytest tests/unit/explore/ tests/integration/explore/ -v` collects and passes **61** tests
(57 + 4), matching the file-by-file count exactly. The ledger's "56/56 ... 60 new tests" is off by
one in both halves of that sentence. Cosmetic — every test genuinely exists and genuinely passes —
but it is exactly the kind of small, checkable ledger claim this project's own Rule E1/tripwire-14
discipline exists to keep honest.

---

## FINDING #5 (LOW) — `_coerce_explore`'s own boundary validation has zero direct unit tests

**Where:** `src/agentdx/config.py:785–825` (`_coerce_explore`); no test file references
`ExploreConfig`, `_coerce_explore`, or `[explore]` directly (`grep -rl "ExploreConfig\|
_coerce_explore" tests/` — no matches).

`CONTEXT.md`'s P13 paragraph claims `tests/unit/sdk/test_config.py` + `tests/unit/store/
test_threshold_config.py` "still pass, 23/23, confirming `config.py`'s extension didn't break
existing config resolution" — which is true and honestly scoped (it is regression coverage that
*other* sections still work, not a claim that `[explore]`'s own validation is tested). But no test
anywhere directly exercises `_coerce_explore`'s five checks (`delay_bound_k` 0–5, `schedule_cap_n`
1–10000, `time_budget_s` > 0, `strategy` ∈ {three names}, `upgrade_reduction_if_redundancy_over`
0.0–1.0).

Live-verified the logic is nonetheless fully correct — 9 boundary probes against an isolated
config file (not the repo's own `agentdx.toml`, which sets matching defaults and would otherwise
mask a broken argument-layer override, a mistake this audit made and caught on the first pass):

```
delay_bound_k=6            -> ConfigError: must be between 0 and 5, got 6
delay_bound_k=-1           -> ConfigError: must be between 0 and 5, got -1
schedule_cap_n=0           -> ConfigError: must be between 1 and 10000, got 0
schedule_cap_n=10001       -> ConfigError: must be between 1 and 10000, got 10001
time_budget_s=0.0          -> ConfigError: must be > 0, got 0.0
time_budget_s=-5.0         -> ConfigError: must be > 0, got -5.0
strategy='bogus_strategy'  -> ConfigError: must be one of [...], got 'bogus_strategy'
upgrade_reduction...=1.5   -> ConfigError: must be between 0.0 and 1.0, got 1.5
upgrade_reduction...=-0.1  -> ConfigError: must be between 0.0 and 1.0, got -0.1
```

All 9/9 correctly rejected. This finding is a coverage gap, not a functional defect — filed at LOW
because the risk it represents (an un-noticed regression in these bounds later) is real but the
current behavior is correct.

---

## FINDING #6 (LOW, structural note) — `.importlinter`'s `explore/` contract is looser than `CONTEXT.md` §4's table implies

**Where:** `.importlinter:112–122` (`explore-below-transport`); `CONTEXT.md` §4's architecture-map
table (`explore/` row: "May import: `runtime`, `analysis.race` | Must not import: —").

The contract's own comment is candid about this: *"'May import: runtime, analysis.race. Must not
import: —.' The table lists no restriction, so only the universal one is encoded: nothing above
the transport layer is imported."* The `forbidden_modules` list is exactly `agentdx.api`,
`agentdx.cli` — it does not positively restrict `explore/` to importing only `runtime`/
`analysis.race` the way `scenario-is-declarative`'s contract explicitly forbids `scenario/` from
importing `runtime`/`store`/`sdk`/`analysis`/`explore`/`api`/`cli` all by name. Currently harmless
— `explore/`'s actual imports are exactly `agentdx.events.schema`, `agentdx.analysis.race`,
`agentdx.explore.*`, and the top-level `agentdx.wall_time` accessor (verified by grepping every
`import`/`from` line in the six source files) — but a future prompt that imports, say, `store/` or
`sdk/` directly into `explore/` (rather than through an injected `ScheduleExecutor`, the pattern
this module currently uses precisely to avoid such an import) would not be caught by CI the way an
equivalent drift in `scenario/` would be.

---

## Positive controls — checked and genuinely correct, not just absence of finding

- **The `decision_step` off-by-one fix holds, verified live against a real `Scheduler`, not just
  by reading `test_decision_step_matches_live_scheduler`.** Live mutation: forced
  `Turn.decision_step` to return `sched_step` (the pre-fix, buggy mapping) instead of
  `sched_step - 1`. Both of the module's dedicated regression tests failed immediately and
  specifically (`test_decision_step_matches_live_scheduler`,
  `test_decision_step_matches_live_scheduler_at_every_branch_point`); nothing else in the suite
  was affected.
- **The `budget_exceeded`/`capped` conflation fix holds, verified live.** Wrapped `generate.explore`
  to re-fold the two signals back into one (`budget_exceeded = capped or budget_exceeded`,
  reproducing the pre-fix behavior described in `Budget`'s own docstring). Exactly
  `test_explore_cap_reached_never_sets_budget_exceeded` failed (`assert True is False` on the
  `budget_exceeded` field); `test_explore_natural_completion_at_exactly_n_is_not_capped` and every
  other `test_generate.py` test stayed green.
- **The I10 honesty guard is real, type-level enforcement, verified live, not convention.**
  Monkeypatched `Report.__post_init__` to a no-op — a `Report` with an arbitrary
  `coverage_statement` then constructs without error. Running `test_report.py` against that live
  mutation fails exactly `test_report_rejects_construction_with_wrong_coverage_statement`
  (`Failed: DID NOT RAISE ValueError`); the other 10 tests in the file stay green. `format_report`/
  `to_api_payload` both thread `report.coverage_statement` (never a separately-typed literal), so a
  future edit cannot drift the CLI-text and API-payload copies apart from the type-enforced field.
- **Dedup hashing has real discriminating power, verified live.** Forced `schedule.signature` (and
  its re-imported name inside `dedup.py`) to always return the same digest regardless of content.
  Exactly `test_len_counts_distinct_signatures_only` and `test_signature_distinguishes_different_
  content` failed; the other 16 tests across `test_dedup.py`/`test_schedule.py` stayed green.
- **`reduce.py` implements exactly the v1 independence-based reduction it claims — no more, no
  less.** `upgrade_reduction_if_redundancy_over` is defined and validated in `config.py` but is
  never read anywhere else in `src/` (`grep -rn upgrade_reduction_if_redundancy_over src/` returns
  only its definition and its one docstring mention in `reduce.py`) — confirms "nothing in this
  build reads that value to switch algorithms" is literally true, not just narrated. Guards 1–3
  are exactly PRD §15.4's pseudocode, reproduced with real `EventType` members. The "fails open"
  empirical proxy (comparing a chosen task's real ops against a *candidate's most recently
  observed earlier turn*, since no static analysis in this codebase currently exposes what an
  unchosen candidate would do) is honestly disclosed, in the module docstring and in
  `docs/exploration.md`, as an approximation with a named, real, non-hidden limitation — not
  claimed to be a full DPOR-grade proof. Its "never leak a later turn" property has a dedicated
  regression test (`test_interesting_steps_only_looks_strictly_earlier_never_future`), read and
  confirmed correct by inspection of `interesting_steps`' own incremental dict-building order.
- **`explore()`'s `capped`/`budget_exceeded` accounting is correct in every edge case checked by
  hand.** Traced the loop's ordering (cap check before budget check, both before popping the next
  frontier item) against `test_explore_natural_completion_at_exactly_n_is_not_capped` (exactly-N
  completion is not "capped") and `test_explore_cap_reached_never_sets_budget_exceeded` (a
  generous 9999s budget with `schedule_cap_n=1` reports `capped=True, budget_exceeded=False`) —
  both match the code's own stated guarantee.
- **The `test_research_fanout_shaped_g2_holds_across_entire_k2_frontier` cross-check is real, not
  tautological.** Its `_reference_frontier` helper is a genuinely independent depth-first
  traversal built from the same *lower-level* primitives (`reduce.interesting_steps`,
  `schedule.Turn.decision_step`, `schedule.signature`) but never calling `generate.explore()`
  itself — a bug specific to `explore()`'s own BFS bookkeeping (e.g. an over-aggressive duplicate
  check silently shrinking the frontier) would show up as a signature-set mismatch between the two
  traversals, which the test asserts directly, not just "more than one schedule ran."
- **The "schedule-invariant finding presence" limitation of both synthetic harnesses is disclosed
  honestly, not hidden.** Both `tests/integration/explore/_harness.py`'s module docstring and
  `docs/exploration.md` state plainly that neither hand-built scenario declares a `causes=` edge
  between writers, so `detect_conflicts`'s vclock-based verdict is the same at every schedule in
  the frontier regardless of interleaving — meaning DoD item 2's "G2 holds across the entire k=2
  frontier" demonstrates the report pipeline runs the detector honestly across every schedule, not
  that bounded exploration discovered a schedule-dependent race no single run would have. This
  matches PRD §15.4's own honesty requirement in spirit and is exactly the kind of caveat this
  project's history (P11-second, §13) has previously had to find independently in other modules —
  here it is already stated by the build itself.
- **`ExploreConfig`'s defaults, in three independent places, agree exactly:** the dataclass
  (`delay_bound_k=2, schedule_cap_n=200, time_budget_s=120.0, strategy="delay_bounded",
  upgrade_reduction_if_redundancy_over=0.40`), `agentdx.toml`'s `[explore]` section (same five
  values, same comments citing the same PRD ranges), and a real `AgentDXConfig.load()` call in
  this sandbox returning the identical `ExploreConfig(...)` repr. `_coerce_explore`'s range checks
  (0–5, 1–10000, >0, three-way choice, 0.0–1.0) match PRD §15.1's table exactly, not just
  approximately.
- **No scope creep.** `strategy="random"`/`"replay_set"` are declared in config but not
  implemented anywhere in `explore/` (confirmed: `generate.py`'s `explore()` has no branch on
  `strategy` at all — it always runs the one delay-bounded BFS) — matches `docs/exploration.md`'s
  own "What is not built, and why" item 2, and D-54's own framing of the config extension as a
  disclosed deviation, not a silent overreach.
- **No I1/determinism-hygiene violation in `explore/`.** `check_determinism_hygiene.py`'s AST-based
  scan (not a grep — it resolves import aliases) reports zero violations under `explore/`; the
  three bare `set()` calls the build's own account says it found and fixed
  (`generate.py`/`reduce.py`/`report.py`) are confirmed gone by direct reading — `generate.py` uses
  a `frozenset(interesting_steps(...))` for pure membership testing, `reduce.py` does the same for
  `reduction_stats`, and `report.py` uses a plain `list` (`seen_finding_ids`) with its own comment
  explaining why a `set` would trip the hygiene check for no benefit (only ever `in`-tested and
  appended, never iterated).

---

## What was checked and holds up

- `docs/exploration.md`'s "Definition of done, demonstrated" block's literal example numbers (6
  schedules/3 reduced/3 duplicate/2 findings for the pipeline-shaped scenario; 13/43/12/0 for the
  fanout-shaped one) match what `pytest -v tests/integration/explore/test_dod.py` actually produces
  in this sandbox — re-ran the DoD suite standalone and confirmed both scenarios' `Report` objects
  print exactly this shape (schedules-executed/reduced-away/duplicate counts line up with the
  committed doc text, field for field).
- `report.py`'s reuse of `analysis.race.detect_conflicts` is real, not a re-implementation —
  confirmed by import (`from agentdx.analysis.race import Finding, detect_conflicts`) and by the
  fact `build_report` calls it once per executed schedule with no intervening logic that could
  itself produce a finding.
- The PRD §15.3 pseudocode's `run.choices_at(step)` and `for alt in range(1, choices_at)` map
  exactly onto `Turn.choices_at`/`generate.py`'s own loop — read side by side, no drift.
- `agentdx.toml`'s `[explore]` section sits correctly ordered among the file's other sections and
  is real, parseable TOML (confirmed via a live `tomllib.loads` read, not just visual inspection).

---

## NOT DONE / RISKS

- **This audit did not attempt to close D-55 itself** (out of scope — read-only, and the
  assignment explicitly said not to re-litigate the scope boundary). Finding #2's live
  demonstration is offered as evidence for whichever future prompt does attempt it, not as a
  half-finished repair.
- **The literal `research_fanout` fixture was not run end-to-end in this audit** — only
  `code_pipeline` was (chosen because it is the smaller, faster fixture and the assignment's own
  goal was to test the *general* D-55/D-80 interaction, not benchmark every fixture). No reason to
  expect `research_fanout` behaves differently; not independently confirmed here.
- **The `crdt_keys` parameter of `build_report`** (passed straight through to `detect_conflicts`)
  was read but not independently exercised with a non-empty value in this audit; its own tests
  live in P12's `analysis/race` suite (already `VERIFIED`), out of this module's own scope to
  re-test.
- **Full-tree `mypy --strict src/agentdx/` remains blocked in this sandbox** by two pre-existing,
  already-disclosed, unrelated gaps (`api/models.py` PEP 695 syntax, `scenario/loader.py`'s missing
  `types-PyYAML`) — corroborated only at the `explore/`+`config.py` scope (7 files, clean), not
  tree-wide. This matches the build's own account of a 31-error tree-wide run this audit could not
  reproduce for a different, sandbox-specific reason (D-66's stub-availability variant, not a new
  claim).
- **Findings #1, #3, #4, #5, #6 are all small enough that a single follow-up prompt could close all
  five in under an hour**; none is blocking. Finding #2 needs a `CONTEXT.md` ledger correction at
  minimum (a D-55 append-correction per `AGENTS.md` §10) even if the underlying wiring work stays
  deferred.
