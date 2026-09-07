# OP-3 REPAIR REPORT — P09 `runtime/faults/` (second cycle)

**Date:** 2026-09-07
**Module / prompt:** `runtime/faults/` (4 MVP fault-injection engine + I12 chaos safety), prompt P09
**Audit this repairs:** `op2-audit-p09-second.md` (independent OP-2, fresh agent, 2026-09-07) — VERDICT FAIL
**Repaired by:** the orchestrating session (not the audit agent), per this project's standing OP-2/OP-3 split

---

## 0. Why this report opens differently from every prior one

The audit's headline finding was not a code defect in the usual sense: the entire first OP-3
repair for this module (`op3-repair-report-p09.md`, 2026-08-17, self-reported "done,
mutation-verified") was **absent from this repository**. Every finding the *first* independent
OP-2 (`op2-audit-p09.md`) had demonstrated and rejected reproduced identically against the live,
unmodified tree — only D-49's separately-committed schema migration had actually landed.

Given the systemic implication (if one module's "repaired" ledger status was false, how many
others might be?), the owner was asked via `AskUserQuestion` how to proceed rather than repairing
immediately. The answer: **"Sweep-check first, then repair."** Before touching any code, the
orchestrating session:

1. Read `CONTEXT.md` §5 in full and identified every row marked "repaired... not `VERIFIED`."
2. For each one, grepped the actual tree for the specific functions/constants/tests its own OP-3
   report claims landed — not trusting the ledger's prose.
3. Traced P09's root cause via `git log --all`/`git show`: a squash-recovery commit (`1b22884`,
   itself reauthoring three prior device-local commits — `9218e67`, `7f93b39`, `b8165dd`, none
   pushed) captured "the actual working tree" for P08/P09/P10/P11 at a moment that post-dated
   P09's repair being *built* but not its *application* — the commit's own message names "P10
   (analysis/resilience.py **+ repair**)" but only "P09 (faults/)," no "+ repair," a distinction
   its own author's wording already carried.

**Result: P05, P06, P07, P08, P10, P11, and P12 all checked out real.** P12 was the row
structurally most at risk, given its own `_incoming/`-tarball delivery mechanism (`556c9a9`'s
commit message: "delivered as a sequence of tarballs... but never integrated into this tree" until
that commit) — its claimed fix (`build_causality` reading `event.vclock` directly rather than
re-deriving from `causal_parents`) is genuinely present, confirmed by direct read. **P09 alone was
affected.** This is now recorded as **D-84** (CONTEXT.md §9), a second confirmed instance of the
failure class D-64 already named for P15-17, via a related but distinct mechanism.

With the scope confirmed isolated, this report covers the actual repair: six findings (3
CRITICAL, 2 HIGH, 1 MEDIUM) from `op2-audit-p09-second.md`, all repaired same day.

---

## 1. Methodology note

This cycle used the stdlib-compat-shim technique first discovered during the P07-second cycle
(`sitecustomize.py` on `PYTHONPATH` backporting `tomllib`/`datetime.UTC`/`enum.StrEnum`) —
**every verification claim below is a real, live execution**, not a proxy: real `pytest`, real
`ruff`, real `mypy --strict`, real `lint-imports`, real `check_ledger.py`/`check_bench_markers.py`/
`check_determinism_hygiene.py`. Three findings were additionally confirmed decisive via live
mutation (backup → mutate → run real suite → confirm red → restore → confirm green;
`git status --short` clean after every restore). This does not close D-66: `api/models.py`'s PEP
695 syntax remains unparseable by this sandbox's CPython 3.10 parser, so `tests/api/` and a
project-wide `mypy --strict`/`check_determinism_hygiene.py` run still cannot include it.

---

## 2. Findings and repairs

### Finding #1 (CRITICAL) — `agent_crash`'s "cannot crash the last live agent" Safety row

**Repair.** `process.py::CrashInjector` gained `_known_live_agents()` (agents ever seen in a
`pre_schedule` `runnable` list, minus already-crashed ones, minus the scheduler's own synthetic
`"root"` task identity) and `_would_leave_no_live_agent(agent_id)`. `_due_fault` now skips
(silently, retried next step — not disarmed) a due `agent_crash` that would leave zero live
agents, unless `armed.decl.params.get("allow_total_failure")` is `True`. `pre_schedule` was
split into two passes — register every runnable task's `agent_id` first, *then* evaluate crash
triggers — so a step that introduces several agents simultaneously for the first time doesn't
undercount live agents based on loop-iteration order.

**A bug in this fix's own first draft, caught by its own regression tests before reaching this
repository's history:** the first version counted the scheduler's own `"root"` bookkeeping task
(every real run's root coroutine is registered as `agent_id="root"`) as a permanently-live
"agent," making the Safety check permanently unsatisfiable for any single-agent scenario. Three
pre-existing tests in `test_process.py` that crash a solo `"reviewer"` as scaffolding for
unrelated mechanics (coro-swap timing, mid-flight interception, taint tagging) broke under this
draft; fixed by excluding `"root"` explicitly, and those three tests updated to declare
`allow_total_failure: true` (their real intent was never to test this Safety row).

**Verified.** Three new tests: single-agent crash with `allow_total_failure` unset (skipped),
explicitly `false` (skipped), explicitly `true` (fires normally) — plus a sanity check that a
crash with a genuine survivor still fires unconditionally. Live mutation: replacing the Safety
check's `if` with `if False:` turns the "skipped" tests red immediately.

### Finding #2 (CRITICAL) — `latency`/`message_drop`'s remaining Safety rows

**Repair.** `TransportFaultInjector.__init__` gained `max_virtual_duration_ms: int | None = None`
(the run's own abort-guard budget); `decide_latency` clamps its proposed delay to
`min(applied, max(0, max_virtual_duration_ms - virtual_ts_ms))` when given. `decide_drop` gained
`carries_run_end: bool = False`, returning `DropDecision(armed=None, dropped=False)`
unconditionally and immediately — before any trigger/probability evaluation, so it never touches
`self._stream` (preserving I1 regardless of how often `run_end` deliveries occur).

**Verified.** New tests: delay clamped when the raw proposal would exceed the remaining budget,
clamped to zero once the budget is already exhausted, unbounded when no budget is given (behavior
preserved); `carries_run_end=True` never drops and produces no event; a non-`run_end` delivery on
the same edge is unaffected.

### Finding #3 (CRITICAL) — fire-time reauthorization had zero real discriminating test coverage

**Repair.** No production code changed — `safety.reauthorize` was already called correctly at all
four sites (`process.py`, `transport.py`×2, `dependency.py`); the gap was entirely in test
coverage. Added one integration-style test per fault class that arms a fault inside the blast
radius, **narrows the live `FaultRegistry.blast_radius` after arming** (`FaultRegistry` is a
plain, non-frozen dataclass; only `BlastRadius` itself is frozen, so
`registry.blast_radius = BlastRadius(...)` is the correct narrowing mechanism), then fires and
asserts `ChaosAuthorizationError`. Added a `BlastRadius.contains` test parametrized over all five
`TargetKind` members, both in-radius and out-of-radius.

**Verified, live mutation, both directions the audit demonstrated:**
- Commenting out `process.py`'s `safety.reauthorize(armed, self._registry.blast_radius)` call
  (`sed`, backup/restore) turns `test_fire_time_reauthorization_refuses_a_crash_whose_radius_
  narrowed_after_arming` red (`DID NOT RAISE ChaosAuthorizationError`) — confirmed, then restored,
  confirmed green again.
- Forcing `BlastRadius.contains`'s `TOOL` branch to always return `True` (not re-tested this
  cycle beyond the new parametrized test's own construction, since the new test enumerates every
  branch directly by construction rather than needing a live mutation to prove coverage — the
  test itself would fail on a `TOOL`-authorization-always-true regression by definition, having
  its own explicit `TOOL` in-radius/out-of-radius case pair).

### Finding #4 (HIGH) — the determinism gate structurally could not exercise `FaultRandomStream`

**Repair.** Two new tests, neither derived from calling `next_permille()` on the "expected" side
(the tautology the audit found in the existing
`test_probability_trigger_matches_stream_draw_exactly`):
- `test_triggers.py::test_seeded_stream_42_matches_independently_computed_reference_values` pins
  `seeded_stream(42)`'s first 10 draws against literals computed in a standalone script directly
  against `hashlib.blake2b`, matching this module's own documented algorithm — never by calling
  `FaultRandomStream.next_permille` itself.
- `test_dependency.py::test_probability_triggered_tool_failure_matches_the_pinned_permille_
  sequence` arms a real `PROBABILITY`-triggered `tool_failure` (a fault type that genuinely
  supports this `TriggerKind`, unlike `agent_crash`) at seed 42 and compares its real fire/no-fire
  sequence against the same independently-computed permille values.
- A companion reproducibility test confirms two fresh, identically-seeded injectors produce
  identical fire sequences (I1), and that the sequence is genuinely decisive (neither always-fire
  nor never-fire).

**Verified, live mutation, exactly the class of bug the audit named:** changing `next_permille`'s
`% 1000` to `% 100` (`sed`, backup/restore) turns both new tests red — the pinned-reference test
fails its literal comparison directly; the tool_failure test's fire/no-fire sequence diverges at
index 1. Restored and reconfirmed green. The pre-existing tautological test was left in place
(unchanged — it still correctly proves `should_fire` consumes exactly one draw per evaluation,
which is a real, separate property) rather than deleted, since it documents something true.

### Finding #5 (HIGH) — abort-guard trips landed in `FAILED`, never `ABORTED_GUARD`

**Repair.** `runtime/scheduler.py::Scheduler.run()` gained `except AbortGuardTripped:` (imported
`from agentdx.runtime.faults.safety import AbortGuardTripped` at module level — confirmed no
circular import: `safety.py` imports nothing from `runtime.scheduler`, directly or transitively;
confirmed no layer-contract violation via `lint-imports`, since both modules live inside the
`runtime/` package and `.importlinter` has no intra-package restriction), ordered before the
existing generic `except BaseException:`, transitioning to `RunState.ABORTED_GUARD` instead of
`FAILED` before re-raising. `safety.py::AbortGuardTripped`'s own docstring, which previously
described this exact gap as NOT DONE, is updated to describe the fix and the residual scope
(injector disarm / in-flight task cancellation, PRD §13.6's other two clauses, is left disclosed
rather than folded in — `runtime/scheduler.py`'s task-lifecycle internals already carry five
concurrency ADRs this session, ADR-017 through ADR-022, and touching them as a side effect of
this narrower fix was judged out of proportion to the fix itself).

**Verified.** The existing `test_a_tripped_abort_guard_stops_the_run_and_the_partial_log_
survives` integration test gained an assertion that `scheduler.state == RunState.ABORTED_GUARD`
after the trip — this is a real, live assertion against the real `Scheduler`/`CrashInjector`, not
a unit-level mock; it passed on the first run once the fix landed and was not additionally
mutation-tested (the fix is a straightforward control-flow addition whose only alternative outcome
— falling through to the pre-existing generic handler — is exactly what the test would have shown
before the fix, which it did, live, before this repair started).

### Finding #6 (MEDIUM) — disagreeing tie-break sentinels; `compute_full_taint` never implemented

**Repair.** `taint.py` gained a shared `_UNKNOWN_INJECTION_SENTINEL: Final[int] = 1 << 62`,
consumed by both `compute_causal_taint` (previously defaulted to `event.seq`) and
`FaultTaintTracker.resolve` (previously defaulted to `1 << 62` already — only the offline function
disagreed). Also implemented `compute_full_taint(events) -> dict[int, frozenset[str]]`, the
D-46-approved (2026-08-17, never implemented against this tree until now) offline function
returning every contributing `fault_id` per event, walking the identical causal graph
`compute_causal_taint` already walks in the same single pass.

**A determinism-hygiene violation in this fix's own first draft, caught before commit:**
`compute_full_taint`'s first version built its per-event contributing set via
`contributing: set[str] = set()` plus an accumulation loop — `scripts/check_determinism_
hygiene.py` correctly flags any bare `set(...)` call under `src/agentdx/` (AGENTS.md §4.1),
including this one, since its own AST scan cannot see that the particular set is safe. Rewritten
as `frozenset[str]().union(*(full[p] for p in event.causal_parents if p in full))` — no `set(...)`
call anywhere, `frozenset(...)` is on this script's own `ORDER_SAFE_WRAPPERS` list. A second,
identical violation in `process.py`'s Finding #1 fix (`known = set(self._task_agent.values())`)
was fixed the same way (`frozenset(...)` directly, no intermediate mutable set, no `.discard`
needed since the exclusion is now expressed as a filter condition instead).

**Verified.** New tests: `compute_full_taint` matches `compute_causal_taint`'s singleton case for
a single fault; keeps *both* contributing faults where the earliest-wins function collapses to
one (the direct PRD §9.4 contrast this function exists to provide); propagates the union
transitively to a third-generation downstream event; returns an empty mapping for an untainted
log. `scripts/check_determinism_hygiene.py` re-run clean after the fix (only the standing, unrelated
`api/models.py` PEP 695 parse failure remains).

---

## 3. Full verification, this cycle

- `tests/{unit,integration}/faults/`: **116/116 passed** (94 pre-existing + 22 new), real
  execution via the shim, `PYTHONHASHSEED=0`.
- Full suite, `pytest --ignore=tests/api`: **2231 passed, 5 failed** — the standing D-66 baseline
  (`test_doctor` + 4 fresh-subprocess tests in `tests/analysis/race/`, `tests/determinism/`,
  `tests/integration/faults/`, `tests/unit/scenario/`, whose own explicitly-constructed
  subprocess `env=` doesn't inherit the shim) — identical failure set to the P07-second cycle's
  baseline, confirming no new regression anywhere in the tree.
- `ruff check` / `ruff format --check`: clean, real, on every touched file.
- `mypy --strict` (scoped to touched files, `--cache-dir=/tmp/mypy_cache_p09` working around the
  sandbox's cache-permission issue): clean on every file this repair changed. Two mypy errors
  remain in `test_process.py` (lines 223-224, 258-259) and several in
  `tests/unit/events/factories.py` — confirmed pre-existing, unmodified by this repair (`git diff`
  shows zero changes to those specific lines).
- `lint-imports`: real, **10/10 contracts kept** — the new `scheduler.py` → `runtime.faults.safety`
  import is intra-package (`runtime/`), not a cross-layer violation.
- `scripts/check_ledger.py`: OK.
- `scripts/check_bench_markers.py`: OK.
- `scripts/check_determinism_hygiene.py`: clean except the standing `api/models.py` PEP 695
  parse failure (D-66, unaffected by this repair) — the two violations this repair's own first
  draft introduced were caught and fixed before this final run.
- Three live mutation probes (Finding #1's Safety check, Finding #3's `reauthorize` call,
  Finding #4's modulus) each independently confirmed to turn the relevant new test(s) red, then
  restored and reconfirmed green — `git status --short` clean after each restore, no residue.

---

## 4. Standing status

**Not `VERIFIED`.** A third independent re-audit is owed, same standing pattern as every module
in this project since P02. Unlike the first repair, this one has been genuinely, live-verified
throughout — including three decisive mutation probes — but it is still self-verified by the
orchestrating session, not independently confirmed by a fresh, memory-free agent.

**Carried forward, not addressed this cycle:**
- PRD §13.6's "injector disarms" / "in-flight tasks are cancelled" clauses (Finding #5's
  disclosed residual gap).
- `fault_summary` persistence (D-47) and the `runtime/`→ `scheduler.py` `CacheHook`-shaped wiring
  question for transport/dependency faults' real interception points — both pre-existing,
  already-declared gaps, unrelated to this cycle's findings, not re-litigated here.
- D-84's own open question: no automated step yet exists to catch a future squash-recovery commit
  silently dropping a repair the way this one did, for any other module.
