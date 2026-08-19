# Race detection (`agentdx.analysis.causality`, `agentdx.analysis.race`)

PRD §14 (causality graph, race detection), §9.4 (fault taint), §9.5 (event payload schemas).
Status: **BUILT — repaired after an independent OP-2 FAIL (2026-08-19)**, self-reported passing
again but a **second independent OP-2 audit is still owed** before this prompt may be marked
`VERIFIED` (see `CONTEXT.md` §5 row 12 and §13's OP-2 / OP-3 rows for the full history, and this
doc's own [Honest recall and coverage statement](#honest-recall-and-coverage-statement) for what
"built" does and does not claim). The first build of `causality.build_causality` had a real bug
— see the next section — found by that audit, not by this build's own test suite.

## The one rule everything else follows from

**A race detector that reports races in correct code is worse than no detector at all.** The
first false positive it ever produces is the last report a user trusts. Every design choice
below — the four guards, the dedup rule, the refusal to implement §14.6's "unavailable value
hash, reduced confidence" row — follows from protecting precision (I5: precision = 1.0 on the
labelled benchmark) even where that costs recall. Recall is reported honestly as possibly
incomplete; it is never inflated to make the tool look more thorough than it is.

## The two graphs — never conflated

`analysis.causality` builds the **happens-before graph**: vector clocks, used only to answer
"could `a` have influenced `b`, or are they concurrent?" This is a different graph from
`analysis.timing`'s **critical-path DAG**, which additionally includes data dependencies and
exists to answer "what is on the critical path?" `race.py` imports `causality`, never `timing`,
and `causality.py` imports neither `timing` nor anything else outside `agentdx.events` — the
two graphs share no code, by design (PRD §14.1).

## Vector clocks: one algorithm, not five — and the bug that shipped in the first build

PRD §14.2 names five rules (local, send, receive, lock, barrier). Conceptually,
`causality.build_causality` implements one: for each event, merge every one of its causal
parents' already-computed vector clocks into the clock last seen on the event's own slot
(`clock_slot` if the event declares one, else `agent_id`, else the run-scope fallback), then
increment that slot by one. This is provably equivalent to all five named rules, because a
message's true causal parent is its sender, a lock acquire's is the matching release, a
barrier release's are every other participant's pre-barrier event — `causality.py`'s own
docstring traces this equivalence in detail.

**The first build got the *source* of "causal parents" wrong, and it was a real bug, not a
style choice.** `build_causality` originally re-derived every event's clock by walking
`Event.causal_parents` — but `runtime.scheduler.Scheduler` stamps that field with a synthetic
linear `[seq - 1]` fallback on *any* event with no declared `causes` (PRD §9.3's log-continuity
chain, unrelated to causation), and no `state_read`/`state_write` in this codebase ever
declares `causes` (`sdk/generic.py`'s `StateHandle` never passes one). The result: every state
operation's `causal_parents` silently absorbed the fallback, so `build_causality` computed two
genuinely concurrent, unrelated-agent writes to the same key as happens-before-ordered —
`race.find_conflicts` reported **zero** conflicts for PRD §23.1's own flagship lost-update
scenario. This was caught by an independent OP-2 audit (`CONTEXT.md` §13, 2026-08-19), not by
this build's own test suite, because every hand-authored test in the suite *also* set
`vclock={}` paired with `causal_parents=()` — a shape the real scheduler never produces (an
empty tuple, not the `[seq-1]` fallback) — so the tests exercised a case the bug could not
touch.

**The fix:** `build_causality` now trusts `Event.vclock` directly. That field is already
computed correctly from declared-`causes`-only semantics by both
`Scheduler._compute_vclock` (the real runtime) and `fixtures._harness.VClockBuilder.build` (the
golden-fixture harness) — neither has a fallback, because the fallback exists only to keep
`causal_parents`' hash-chain contiguous, and `_compute_vclock` never touches that field. Reading
the stamped clock verbatim (after pruning any zero-valued slots) is simpler than re-deriving it
and cannot reintroduce this class of bug, because it no longer looks at `causal_parents` — the
field carrying the fallback — at all. `tests/analysis/race/test_causality.py`'s parametrized
cross-check (153 events across every real, committed golden log) now confirms `build_causality`
reproduces the runtime's own stamped clock exactly, and a dedicated regression test
(`test_op2_20260819_unrelated_writes_stay_concurrent_with_fallback_causal_parents`) builds a log
with the exact fallback shape the first build missed and asserts the pair stays concurrent. The
shared `tests/analysis/race/_causal_log.py` helper now builds every hand-authored test event
with a realistic, fallback-shaped `causal_parents` by construction, so this blind spot cannot
recur test-by-test.

## The detection pipeline

```
events -> causality.build_causality -> KeyState tracking (§14.3) -> raw conflicts (§14.4)
        -> value divergence (§14.6) -> the four guards (§14.7) -> dedupe (§14.3)
        -> Finding (§14.5 classification, I6 evidence, §9.4 fault taint)
```

For each `state_write`/`state_read` event, the algorithm looks for **prior** accesses to the
same key, on a **different** slot, whose vector clock is **concurrent** with the current
event's. A write finds concurrent prior writes (`write_write`) and concurrent prior reads
(`write_read`); a read finds concurrent prior writes (`read_write`). Each key keeps only the
single most recent access per slot (`_KeyState`), so memory is O(live keys × slots), and a
representative — the chronologically latest raced pair — is kept per `(key, subtype, slots)`
group rather than every historical instance (`_dedupe`).

## Classification (PRD §14.5)

| Subtype | Pattern | User-facing name | Base severity |
|---|---|---|---|
| `write_write` | Two concurrent writes to the same key | Lost update | Critical |
| `read_write` | A read concurrent with a write | Stale read | High |
| `write_read` | A write concurrent with an earlier read | Dirty read | High |

`write_write` is critical because data is destroyed, not merely stale. A `write_read` whose
write carries a `txn_id` is a torn read (`torn: true`) and is elevated High → Critical.
Classification is exhaustive by construction: `_raw_conflicts` has exactly two call sites, one
per event-type branch, each passing a literal, fixed subtype — a fourth subtype is not
reachable, and `UnclassifiableConflictError` (`E-RACE-001`) exists only to make a future coding
mistake loud rather than silently mislabelled (Design Constraint 7: "an unclassifiable conflict
is a bug in the classifier, not a new 'other' bucket").

## The four guards (PRD §14.7)

Each is tested independently, in both directions (`tests/analysis/race/test_guards.py`, 8
tests: one proving the guard suppresses the false-positive class it exists for, one proving it
does *not* suppress a genuine conflict sharing a surface feature with that class).

- **G1 Concurrency** — the definition itself. Neither access happens-before the other. This is
  not a post-hoc filter; there is no code path that reaches guard evaluation for a
  happens-before-ordered pair at all.
- **G2 Divergence** — suppress when both accesses hash to the same value. Concurrent, identical
  writes are harmless (idempotent); nothing was lost.
- **G3 Declared merge** — suppress when the write's `payload.reducer` field is non-null, or the
  key is in a caller-supplied `crdt_keys` set. This is the guard that makes `research_fanout` (a
  real LangGraph `Annotated[list, operator.add]` channel) report zero findings instead of six.
- **G4 Explicit synchronisation** — suppress a `write_write` pair when both writes carry the
  same non-null `lock_id`. Scoped to `write_write` only: `state_read`'s payload has no
  `lock_id` field at all (PRD §9.5) — a lock protects a write, and the schema has nothing to say
  about a read's lock membership. Barrier-ordered accesses never reach G4 in the first place —
  a barrier is a real synchronisation primitive in the causality graph, so two barrier-ordered
  accesses already fail G1.

Suppressed conflicts are not discarded. `find_conflicts` returns every conflict, reported or
not, each carrying `suppressed_by`; `detect_conflicts` (most callers want this one) filters to
`suppressed_by is None`. The two can never diverge — `detect_conflicts` is defined as a filter
over `find_conflicts`' own output, one code path, not two.

## Fault taint (PRD §9.4)

`race.py` does not know what a fault *is* — I3 purity forbids importing `runtime`. It reads
`Event.fault_id`, a field `runtime.faults.taint` (P09) has already fully resolved by stamp time.
A `Finding.fault_id` is the earlier of its two accesses' `fault_id` values, or `None`. A
fault-tainted conflict is **still reported, never suppressed** — fault taint is not a fifth
guard; a race that only manifests because a fault perturbed scheduling is still a real race the
user's code needs to survive. It is *classified* differently only in the sense that a caller
building a scorecard can separate "this system has a concurrency bug" from "this system has a
concurrency bug a chaos experiment surfaced" — the distinction matters for what a user does
next, not for whether the finding is reported.

## Minimal reproduction (PRD §14.8)

PRD §14.8's full algorithm identifies the scheduling decisions that produced a race, computes a
minimal `delay_schedule` that reproduces the ordering, and shrinks it against up to 16 re-runs.
This build has no scheduler to re-run against (`analysis.explore`'s bounded schedule
exploration is P13, out of this prompt's scope) — and the scenario schema this codebase ships
(`scenario.schema.TOP_LEVEL_KEYS`) has no `delay_schedule` field yet regardless.
`race.minimal_repro` therefore performs step 4 only: it pins the run's real `seed`
(`run_start.payload.seed`), targets the finding's fixture, and emits a complete, valid v1
scenario document asserting `no_state_conflicts` — a genuine CI regression test a user can run
once the race is fixed. This is not a weaker stand-in that happens to work by luck: every
fixture this build ships documents its races as reproducing under the default schedule alone
(no scheduler perturbation needed), so step 4 alone is a real reproduction for every finding
this build can currently produce. `tests/analysis/race/test_minimal_repro.py` validates the
emitted YAML against the real `scenario.validate.validate()` (not just "looks like YAML"), and
evaluates the real `scenario.assertions.eval_no_state_conflicts` against `race.py`'s own
`Finding` objects to prove the assertion actually discriminates unfixed from fixed.

## Worked examples

**`fixtures/code_pipeline` — the seeded lost update (gate G1).** `coder` writes
`draft.module_a` at seq 13 with no reducer, no lock, and no `await` before it; `reviewer`
writes the same key at seq 28, after one scheduling yield. Neither write's `causal_parents`
names the other, and their vector clocks are concurrent. Values diverge (different edits); no
guard applies. `detect_conflicts` reports exactly one finding: `write_write`, `critical`,
`coder`/`reviewer`, evidence seq `(13, 28)`, `reviewer`'s write (seq 28) surviving, `coder`'s
(seq 13) silently discarded — matching `fixtures/code_pipeline/golden_findings.json` exactly.

**`fixtures/research_fanout` — the healthy control (gate G2 / invariant I4).** Four workers
concurrently write the same `findings` key, a real LangGraph `Annotated[list[str],
operator.add]` reducer channel — every write's `payload.reducer` is `"operator.add"`. All
`C(4,2) = 6` pairs are genuinely concurrent and genuinely divergent (each worker contributes
different findings) — G1 and G2 both let them through. G3 catches every one: `detect_conflicts`
reports zero findings, `find_conflicts` shows all six raw conflicts present and
`suppressed_by == "G3"`. `tests/false_positives/test_research_fanout.py` proves this against
the real golden log, across 100 replays; `tests/false_positives/test_k2_frontier.py` proves it
again across every schedule reachable by the k=2 frontier (see below).

## Honest recall and coverage statement

This section states plainly what this build's race detector does and does not cover — the
mission's own instruction, restated here for a reader who did not see the mission brief.

**What is verified.** Precision on every fixture and hand-authored test this build has: **1.0**
— zero false positives across `tests/analysis/race/` (64 true-positive and guard tests, all
passing) and `tests/false_positives/` (13 tests, gate G2's mandatory suite, including the real
`research_fanout` fixture across 100 replays and the k=2 frontier). Gate G1 (`code_pipeline`'s
seeded lost update) is found exactly as specified. All PRD §33.8 true-positive rows are covered
(`tests/analysis/race/test_true_positive_matrix.py`), with one documented exception below.
Analysis is deterministic: 100 in-process replays byte-identical, and — the stronger claim — 8
fresh-interpreter replays at varied `PYTHONHASHSEED` are also byte-identical, meaning no bare
`dict`/`set` iteration order is silently leaking into the result (NFR-14). Every hand-authored
event in this suite is now built through `tests/analysis/race/_causal_log.py`'s `CausalLog`
helper, which reproduces the real scheduler's `causal_parents` fallback shape by construction
(see the OP-2 item below) — so "passing" no longer means "passing against an unrealistic log
shape," which is exactly what it meant before 2026-08-19.

**What is not built, and why.**

0. **A second independent OP-2 audit is owed and has not yet happened.** The fix described
   above (trust `Event.vclock`) was chosen by the project owner from three options after an
   independent audit found the first build's `build_causality` silently defeated race
   detection for every state operation (`CONTEXT.md` §13's OP-2 row, 2026-08-19); the repair —
   `causality.py`, `race.py`'s docstring, nine test files, and this document — was then made and
   self-verified (`CONTEXT.md` §13's OP-3 row) but **has not been independently re-audited**.
   Golden-fixture regeneration against a real `runtime.scheduler.Scheduler` + `RunHost` (the
   audit's second-priority fix) is explicitly deferred, not silently dropped: `RunHost` does not
   exist yet in this build (a P06-era gap, already declared elsewhere in `CONTEXT.md`), so there
   is no real scheduler to regenerate fixtures against. Until a second OP-2 confirms the repair,
   treat this module's self-reported PASS with the same caution the mission's own audit protocol
   assigns to any unaudited claim.

1. **The formal 40-log labelled benchmark (PRD §34.3).** "Recall = 1.0 on the seeded set,
   precision = 1.0 on the negative set" is a claim about a specific, committed 40-log corpus
   and a `bench/` harness that publishes a confusion matrix. Neither exists yet — building the
   corpus and the harness is a `bench/`-scoped deliverable this prompt's `DELIVERABLES` do not
   name. What this build *can* honestly claim is precision = 1.0 on every case its own test
   suite covers (above); it cannot yet claim a measured recall number against an independent
   corpus, and does not claim one.
2. **`value_hash` unavailable (PRD §14.6's fourth row).** Not implemented. `value_hash` is a
   required, non-nullable field on both `state_read` and `state_write` payloads (PRD §9.5), so
   no live code path in this build ever produces an event with one absent — there is nothing to
   exercise this branch against today, and inventing a code path that raises a lower-confidence
   finding for a case that cannot currently occur would be speculative, not evidenced.
   `tests/analysis/race/test_true_positive_matrix.py::test_row_11_...` names this gap
   explicitly rather than silently omitting the PRD row.
3. **G3's known precision-favouring gap.** G3 suppresses on the *presence* of a declared
   reducer, not on whether that reducer is actually non-destructive. A channel with a
   last-write-wins "reducer" under a non-null name would be suppressed too. This is a **recall**
   gap (it only ever causes under-reporting, never a fabricated finding), not a precision one,
   and it does not affect either shipped fixture (`fixtures/code_pipeline/README.md` documents
   why the fixture set was built so this distinction does not matter for gates G1/G2 today).
4. **The k=2 frontier is a test-only, bounded-transposition enumeration, not real scheduler
   exploration.** `tests/false_positives/_k2_frontier.py` enumerates orderings reachable by at
   most 2 pairwise swaps from a default order — a deliberately minimal stand-in for PRD §15.1's
   "scheduling points" concept, since no real scheduler exists yet to define one (P13, out of
   scope). It satisfies gate G2 / invariant I4's literal requirement and ADR-002's explicit
   scope ("no CLI, no API field, no UI, no reduction reporting, no coverage panel — a test
   fixture, not an early FR-6"); it is not a claim that this build performs bounded schedule
   exploration as a product feature.
5. **`minimal_repro` implements PRD §14.8's step 4 only** (pin seed, target fixture, assert
   `no_state_conflicts`), not the delay-schedule minimisation in steps 1–3 — see the section
   above for why, and why step 4 alone is still a genuine reproduction for every finding this
   build can currently produce.
6. **`tests/false_positives/` is scoped to the race-detection rows of PRD §33.9's table.** Three
   rows of that table — retry of the same tool call is not redundancy, ≥3-average-parallelism
   fan-out is not reported as fake fan-out, and a single-agent run has no coordination findings
   — are the responsibility of `analysis.redundancy` and `analysis.verdict` respectively, both
   already `BUILT` with their own dedicated test suites (`tests/analysis/test_redundancy.py`,
   `tests/analysis/test_verdict.py`). Re-deriving those cases here would duplicate coverage
   that already exists elsewhere under a different module's own gate, not close a real gap.

**Net honest statement:** this build's race detector has verified precision = 1.0 against
every case it has been tested against (its own true-positive matrix, guard suite, and the
false-positive suite including a real fixture and a bounded schedule frontier), and a recall
claim that is deliberately *not* inflated beyond what a committed, auditable benchmark corpus
would be needed to support — that corpus does not exist in this build.
