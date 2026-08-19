# OP-2 Independent Audit — P12 (`analysis/causality`, `analysis/race`, `tests/false_positives/` k=2 harness)

**Auditor:** independent OP-2 pass, fresh read, no memory of the P12 build session.
**Date:** 2026-08-19.
**Scope:** `src/agentdx/analysis/causality.py`, `src/agentdx/analysis/race.py`,
`tests/analysis/race/`, `tests/false_positives/`, `docs/race-detection.md`, and the P12-related
edits inside `CONTEXT.md` (§5 rows 12/12b, §6 gates G1/G2, §7's P12 paragraph, §9 D-52/D-53, §10
C-26, §13's newest row).

**Note on the task brief itself.** The brief describing this audit states `CONTEXT.md` is
"900+ lines." The actual file is 515 lines (`wc -l CONTEXT.md`). It also asks me to match the
format of an existing `op2-audit-p09.md`; no such file exists anywhere in this tree
(`find . -iname "op2-audit*"` returns nothing), despite `CONTEXT.md`'s own P09 build paragraph
citing it by name as if it were a real, inspectable artifact ("34 mutation-tested findings
(`op2-audit-p09.md`)"). Neither of these facts changes the P12 verdict below, but the second one
is itself relevant evidence for §6 (Honesty Audit): this ledger has at least one precedent of
citing a file as evidence that does not exist in the tree it is committed to.

---

## Headline finding, stated up front

**`analysis/causality.py` computes its causality graph from `Event.causal_parents`, but the
real runtime (`runtime/scheduler.py`) stamps that exact field with a synthetic linear
`[seq - 1]` fallback on any event with no declared `causes` — which is every `state_write` and
`state_read` event the SDK ever emits.** The result, demonstrated live below: two state writes
from two unrelated agents, with no lock, no message, no reducer — the textbook lost-update race
this entire module exists to catch — are computed as **happens-before**, not concurrent, and
`race.find_conflicts` reports **zero conflicts**. This is not a corner case; it is the general
case for every state access in the system once fed a log shaped like what the real scheduler
actually produces, as opposed to the simplified fixture-harness logs this build's entire test
suite was checked against. Full derivation in §1 and §4.

---

## 1. SPEC CONFORMANCE

PRD §14 requirements in scope, one row per named requirement:

| Requirement | Status | Evidence |
|---|---|---|
| §14.1 two separate graphs, no cross-import | Implemented | `lint-imports` 10/10 kept; `causality.py` imports only `agentdx.events`; `race.py` imports only `causality`+`events`. |
| §14.1 "shared-state access does NOT create a happens-before edge" | **Contradicted in practice** | See headline finding. The rule is stated three times in `causality.py`'s own docstring (lines 16–23) and once more in `race.py`'s docstring, but the *implementation* violates it whenever the log carries the real scheduler's `causal_parents` fallback, because that fallback silently becomes exactly the edge the rule forbids. See §4 for the live repro. |
| §14.2 vector clock rules (local/send/receive/lock/barrier) | Implemented, but built on the wrong source field | `build_causality` (`causality.py:223-271`) correctly implements the *algebra* (merge-then-bump) for all five named cases. Its defect is upstream of the algebra: it trusts `event.causal_parents` to mean "declared happens-after," when the persisted field also carries a log-continuity artifact (`runtime/scheduler.py:1059-1078`, `_causal_parents`) that is explicitly *not* causation, per that very method's own docstring ("Only when `causes` is empty ... does this fall back to the previous event in the log, giving a linear chain") and per `runtime/scheduler.py:424-437`'s own comment distinguishing "declared" causal parents from `Stamp.causal_parents` for exactly this reason (written earlier, for the fault-taint hook, by P09). `causality.py` never makes this distinction. |
| §14.3 access tracking (`KeyState`-equivalent, last write/read per slot) | Implemented | `_KeyState` (`race.py:371-382`), O(live keys × slots) as specified. |
| §14.4 detection algorithm | Implemented, structurally matches the PRD pseudocode | `_raw_conflicts` (`race.py:533-593`) mirrors the PRD's three branches exactly; correct *given a correct causality graph* — which it is not (see above). |
| §14.5 conflict classification (`write_write`/`read_write`/`write_read`, torn read elevation) | Implemented | `ConflictSubtype`, `_severity_for` (`race.py:688-699`); exhaustive by construction, `UnclassifiableConflictError` unreachable but present as a guard. |
| §14.6 value divergence, 4 rows | Partially implemented, declared | Rows 1–3 implemented (`_diverges`, `race.py:390-402`); row 4 (`value_hash` unavailable → reduced confidence) not implemented, declared honestly in `docs/race-detection.md` item 2 and in `test_row_11_...`. Acceptable: the schema makes `value_hash` non-nullable, so the branch is genuinely unreachable today. |
| §14.7 four guards | Implemented and independently tested in both directions | `_diverges`/`_has_declared_reducer`/`_same_lock`, 8 tests in `test_guards.py`. Mutation-tested live (§4) — G4's `and`/`or` mutation was caught. |
| §14.7 suppressed conflicts retained, not discarded | Implemented | `find_conflicts` returns every conflict with `suppressed_by`; `detect_conflicts` filters. No UI drawer exists yet (out of scope — nothing here claims one). |
| §14.8 minimal reproduction, steps 1–4 | Step 4 only, declared as **D-53** | `minimal_repro` (`race.py:866-933`) pins seed + fixture + `no_state_conflicts` only. The declaration is honest and the reason given (no `delay_schedule` field in the shipped scenario schema, no scheduler to shrink against) is correct and verifiable against `scenario/schema.py`. |
| §14.9 reporting format | Implemented | `format_finding` (`race.py:784-795`) matches the PRD §14.9 example's shape field-for-field (spot-checked against gate G1's own pasted output). |
| §9.4 fault taint — read-only, never suppresses | Implemented correctly | `race.py` reads `Event.fault_id` only, never recomputes; taint is not treated as a fifth guard (`_conflict` has no branch conditioned on `fault_id`). Correct per PRD §9.4 and per the mission's explicit instruction. |

**Net verdict for this section: the single most important rule in §14.1 — the one the module's
own docstring says is "the one mistake that silently defeats the whole module" — is violated by
the implementation's choice of source field, in a way none of the 78 new tests can observe.**

---

## 2. INVARIANT CHECK

| Invariant | Status | Mechanism |
|---|---|---|
| **I1/NFR-14 determinism** | Held | `build_causality`/`find_conflicts` are pure single forward passes; every returned structure is built from sorted, explicit keys, never bare `dict`/`set` iteration order. Verified live: `check_determinism_hygiene.py` clean (67 files); `test_determinism.py`'s 8-subprocess/varied-`PYTHONHASHSEED` claim is real (ran the suite, 78/78 pass under `PYTHONHASHSEED=0`). The headline bug does not threaten I1 — the wrong graph is still computed *deterministically* wrong. |
| **I3 analysis purity** | Held | `lint-imports` 10/10 contracts kept, run live this session. `causality.py`/`race.py` import only `agentdx.events` and each other (one direction). No `runtime`, no `sdk`. |
| **I6 evidence-non-empty** | Held | `Finding.evidence_seq` is always the two racing accesses' seqs, non-empty by construction; `check_fixture_finding_evidence.py` clean. Irrelevant to the headline bug — the bug produces *no finding at all* rather than an evidence-empty one, which is a different (also serious) failure mode I4/mission-thesis territory, not an I6 violation. |
| **I4 zero false positives on the healthy fixture** | Held, but for a reason that also implicates the headline bug | `research_fanout` reports zero findings across 100 replays and the k=2 frontier — confirmed live (13/13 pass). Worth noting: I4 is a *precision* invariant (never report a false race), and the headline bug is a *recall* failure (never report a true race) — it does not violate I4 as written. But a detector that structurally cannot see most real concurrency is not meaningfully "passing" I4 either; it is passing a test it cannot fail. |
| **I5 race precision = 1.0** | Held on the tested set, but the tested set is not representative | Every test in the 78-test suite hand-sets `causal_parents` to the PRD-ideal shape (empty for unsynchronized state ops), never the shape the real scheduler emits. Precision = 1.0 is true *on inputs shaped like the test suite's own construction*, which is not evidence about precision on real runtime output — because on real runtime output, this module currently produces close to zero findings of any kind, reported or suppressed, so there is nothing to be imprecise about. |
| **I13 no model inference** | Held | N/A to this module; no LLM/embedding code present. |

---

## 3. SCOPE VIOLATIONS

No files outside the named deliverables were touched by the code itself. `CONTEXT.md` was edited
only in the sections the prompt names.

**D-52's declaration (test-helper files not itemised in `DELIVERABLES`)** is honest and
sufficiently detailed — it names every helper file (`_events.py`, `_subprocess_runner.py`,
`_k2_frontier.py`) and every multi-row test file, and gives a real, checkable rationale for each
(mirrors `tests/determinism/_subprocess_runner.py`'s existing pattern; avoids duplicating
boilerplate). I checked this by grep rather than trusting the row: every file it lists exists,
and no file exists that it does not list. Verified: `ls tests/analysis/race/ tests/false_positives/`
against D-52's own file list — exact match. This is the *n*th recurrence of the same shape
(D-12/20/38/39/41/43) and the recurring "not reconciled, propose fixing DELIVERABLES conventions
at the next prompt revision" line is now boilerplate that nobody has acted on across six
prompts — a process observation, not a P12-specific defect.

**The k=2 harness (`tests/false_positives/_k2_frontier.py`)** was checked against ADR-002's
explicit constraint ("must never grow a CLI flag, an output format or a reduction report"): it is
one pure function (`k2_frontier`), no CLI entry point, no `__main__`, no argparse, no output
format beyond a Python tuple returned to its one caller (`test_k2_frontier.py`). Tripwire 9b does
not fire.

No refactor of any earlier prompt's code was found (`git`-less tree, so checked via `mypy --strict`
file counts staying at 67 and `ruff` producing no diffs outside the five new/changed files).

---

## 4. THE TEST-QUALITY QUESTION

### Test 1: `tests/analysis/race/test_causality.py::test_build_causality_matches_the_real_stamped_vclock_exactly`

This is the test the build's own docstrings lean on hardest — `causality.py` cites it by name as
the evidence for trusting `causal_parents` over `Event.vclock`, and `docs/race-detection.md`
repeats the claim ("this build validated the two agree exactly across every real, committed
golden log ... 153 events, zero mismatches").

**The bug it would miss:** exactly the headline bug. It compares `build_causality`'s output
against `Event.vclock` on `tests/golden/*.jsonl` — logs produced by `fixtures/_harness.py`, whose
own module docstring states outright: *"`causal_parents` is exactly what the SDK's own
`emit(..., causes=...)` call sites already compute ... This harness adds no causal edges of its
own."* That is, the golden fixtures never carry the real scheduler's `[seq-1]` fallback, because
the harness that produced them deliberately does not implement it — a fact the harness's own
docstring documents as a known, provisional gap (ADR-001: *"their golden corpora are explicitly
provisional — regenerated at P07 once both [runtime/ and runtime/cache/] are real"*).
`fixtures/code_pipeline/golden_findings.json`'s own `"status"` field says the same thing
verbatim: *"PROVISIONAL — recorded before runtime/ (P06) and runtime/cache/ (P07) exist, per
ADR-001. Regenerate at P07."* Per `CONTEXT.md` §5 rows 6–7, both P06 and P07 are now `BUILT` —
the regeneration this file has been waiting for is overdue, and nobody involved in P12 appears
to have checked whether the golden logs it was validating against were still representative of
what `runtime/scheduler.py` actually emits.

**What test would catch it:** none in this suite. I wrote one (see mutation/repro below) — the
minimum needed is a test that builds a log using the *real* `runtime.scheduler.Scheduler`
(or, short of standing up a full scheduler, hand-constructs events with `causal_parents` shaped
per `Scheduler._causal_parents`'s own documented fallback rule) and asserts `concurrent()` is
still `True` for two state writes from different agents with no declared synchronisation. No such
test exists anywhere in `tests/analysis/race/` or `tests/false_positives/` — I grepped every
`causal_parents=` call site in both directories; every single state-op event in all 78 tests uses
`causal_parents=()`, never the fallback shape.

**Live proof, not hypothetical (ran, not mutated — this is the actual as-shipped code):**

```
$ PYTHONPATH="src:tests" python3 - <<'EOF'
from agentdx.analysis.causality import build_causality, concurrent_events, happens_before_events
from tests.analysis.race._events import state_write
from tests.analysis._events import run_start

r0 = run_start(seq=0, virtual_ts_ms=0)
e1 = state_write(seq=1, virtual_ts_ms=10, vclock={}, causal_parents=[0],
                  agent_id="coder", span_id="s1", key="k", value="A")
e2 = state_write(seq=2, virtual_ts_ms=11, vclock={}, causal_parents=[1],
                  agent_id="reviewer", span_id="s2", key="k", value="B")

graph = build_causality([r0, e1, e2])
print("happens_before(e1, e2) =", happens_before_events(e1, e2, graph))
print("concurrent(e1, e2)     =", concurrent_events(e1, e2, graph))
EOF
happens_before(e1, e2) = True
concurrent(e1, e2)     = False
```

`causal_parents=[0]`/`[1]` here is not an invented adversarial input — it is *exactly* what
`Scheduler.write` (`runtime/scheduler.py:411-412`, `_causal_parents`, lines 1059-1078) computes
for any event whose caller passed no `causes`, which `sdk/generic.py`'s `StateHandle.write`/
`_apply`/`read` (lines 1353-1429) always do (`emit(..., causes=())` — confirmed: no state
operation anywhere in `sdk/generic.py` ever passes a `causes` argument).

End-to-end confirmation that this reaches `race.py`'s public API and produces the wrong result on
exactly the shape of PRD §33.8 row 1 / gate G1:

```
$ PYTHONPATH="src:tests" python3 - <<'EOF'
from agentdx.analysis.race import find_conflicts
from tests.analysis.race._events import state_write
from tests.analysis._events import run_start

r0 = run_start(seq=0, virtual_ts_ms=0)
e1 = state_write(seq=1, virtual_ts_ms=10, vclock={}, causal_parents=[0],
                  agent_id="coder", span_id="s1", key="draft.module_a", value="A")
e2 = state_write(seq=2, virtual_ts_ms=11, vclock={}, causal_parents=[1],
                  agent_id="reviewer", span_id="s2", key="draft.module_a", value="B")

print("conflicts found:", len(find_conflicts([r0, e1, e2])))
EOF
conflicts found: 0
```

Two agents, no lock, no message, no reducer, divergent values on the same key — the exact PRD
§23.1 seeded defect gate G1 is built around — produces **zero** conflicts when the log is shaped
the way the real scheduler actually stamps it. `test_gate_g1.py` passes only because it reads
`tests/golden/code_pipeline.jsonl`, which was never run through `runtime/scheduler.py`.

### Test 2: `tests/analysis/race/test_guards.py::test_g4_explicit_lock_does_not_suppress_writes_under_different_locks`

**The bug it would miss, hypothesised then tested for real:** a guard implemented with the wrong
boolean operator (`or` instead of `and`), which would over-suppress — the single most dangerous
mutation class for a precision-critical module, since it directly threatens I4/I5.

**Mutation performed (backed up first, restored after, verified byte-identical):**

```
--- a/src/agentdx/analysis/race.py
+++ b/src/agentdx/analysis/race.py
@@ def _same_lock(write_a, write_b):
-    if write_a.lock_id is not None and write_a.lock_id == write_b.lock_id:
+    if write_a.lock_id is not None or write_a.lock_id == write_b.lock_id:
         return write_a.lock_id
     return None
```

Result: `pytest tests/analysis/race/test_guards.py tests/false_positives/test_lock_protected_writes.py -q -rA`
→ **1 failed** (`test_g4_explicit_lock_does_not_suppress_writes_under_different_locks`), 9 passed.
The "does not suppress" half of the guard's own two-directional test suite (mission Design
Constraint 3: "a guard tested in only one direction is an untested guard") caught it immediately.
Restored via `cp` from a pre-mutation backup; `diff` against the backup confirms byte-identical
restoration.

**A second mutation, on `causality.py`'s boundary check** (`happens_before`'s `<=` weakened to
`<`, a classic off-by-one on the "at most" half of the definition): 4 tests failed
(`test_happens_before_dominates_on_every_slot`, `test_receive_merges_the_senders_snapshot_then_
bumps_own_slot`, `test_barrier_all_to_all_merge_orders_every_participant_after_every_other`,
`test_g1_concurrency_suppresses_a_causally_ordered_pair`) — this class of bug is caught robustly,
by design (multiple hand-computed logs assert exact vclock values, not just pass/fail).
Restored; `diff` confirms byte-identical.

**What this proves:** the guard logic itself (G1–G4, as algebra) is well tested and would catch a
real regression. The suite's blind spot is entirely upstream of the guards — in what counts as
"a causal parent" in the first place — and that blind spot is total: every one of the 78 tests
constructs its own `causal_parents` by hand, always in the PRD-ideal shape, never in the shape
the one component that will eventually produce real logs (`runtime/scheduler.py`) actually emits.
A test suite that is internally consistent and a system that is externally correct are not the
same claim, and this suite only demonstrates the first.

---

## 5. DRIFT TRIPWIRES

Walking `CONTEXT.md` §11 item by item:

1. **Test changed to pass, not code fixed.** Not observed. No test assertions were weakened; the
   row-6 fix (redesigning the log so a third agent receives directly from both others) narrowed
   the *test's own log construction* to isolate a different race, which is a legitimate test
   design change, not a weakened assertion — the assertion (concurrency ⇒ conflict reported)
   is unchanged. Does not fire.
2. **Banned non-determinism under `src/agentdx/`.** `check_determinism_hygiene.py` clean, 67
   files, ran live. Does not fire.
3. **`analysis/` importing `runtime`/`sdk`/a model client.** `lint-imports` 10/10 kept, ran live.
   Does not fire.
4. **Finding/verdict without `seq` evidence.** `check_fixture_finding_evidence.py` clean.
   `Finding.evidence_seq` always non-empty by construction. Does not fire.
5. **Magic number inline instead of config.** `MAX_TRANSPOSITIONS = 2` in `_k2_frontier.py` is a
   named module constant with a citation to PRD §15.1's default `k`, explicitly declared
   "not a CLI flag, not configurable (ADR-002/§11.9b)" — this is deliberate, not a tripwire
   instance, since the whole point of the k=2 harness is a fixed, non-configurable stand-in.
   Everything in `race.py`/`causality.py` that could be a threshold (severity mapping, guard
   conditions) is structural classification, not a tunable magic number. Does not fire.
6. **Event schema modified without an ADR/version bump.** Not touched by P12. Does not fire.
7. **Unmarked number in docs.** `check_bench_markers.py` clean, ran live, 13 files. The "153
   events, zero mismatches" figure in `docs/race-detection.md` is a real, cross-checked count
   (I recomputed it independently: 48+64+41=153) rather than a bare unmarked statistic subject
   to the `[bench:...]` rule (it is a test-count claim, not a performance number, so the rule
   does not apply to it — correctly not marked). Does not fire mechanically, though see §6 for
   why the claim built on top of that number is still misleading.
8. **Panel owning selection state instead of Zustand.** N/A — no frontend code in this prompt.
9. **LLM-as-judge / semantic dedup / scope creep proposal.** Not observed.
   9b. **k=2 harness grows a CLI flag/output format/reduction report.** Checked directly —
   `_k2_frontier.py` has no `__main__`, no CLI, no argparse, returns a plain tuple. Does not fire.
10. **Scope cut taken out of order.** N/A, no cut taken.
11. **Prompt output covers modules its `DELIVERABLES` did not name.** Not observed beyond the
    D-52-declared helper files, which are conventional test infrastructure, not product surface.
12. **Replay-mode cache miss handled by anything but a hard error.** N/A to this prompt.
13. **Row edited/removed from §8 or §9.** Not observed — new rows only (D-52, D-53, ADR entries
    unchanged, C-26 new).
14. **A PRD requirement in scope neither implemented nor declared in §9.** This is the one that
    is genuinely at risk of firing, and it is worth stating precisely why it is a close call
    rather than a clean pass: PRD §14.1's core rule is not "neither implemented nor declared" —
    it *is* implemented, and the module's docstrings assert compliance with it in the strongest
    possible language ("the one mistake that silently defeats the whole module"). The gap is
    that the implementation does not actually satisfy the rule against real runtime output, and
    nothing in `CONTEXT.md` §9 declares this as a known limitation — D-52 and D-53 are about
    scope (undeclared files, an incomplete algorithm step), not about this correctness gap. This
    is not tripwire 14 in its literal form (something un-built and undeclared) but its sibling:
    something built, declared complete, and asserted to hold an invariant it does not actually
    hold against the one real component (`runtime/scheduler.py`) that exists to produce the
    logs this module is meant to analyse.
15. **A workflow on `main` red.** N/A — no CI configured to run in this sandbox (no `.git`).
16. **A recording view subclassing a builtin.** N/A to this prompt — no `dict`/`list`/`set`
    subclassing anywhere in `causality.py`/`race.py`. Does not fire.
17. **A change moves a gate's pass/fail status, narrated only as a bug fix, no §9/§10 row.**
    Does not fire in the literal sense (no gate visibly flipped in this sandbox, since G1's own
    test never touched a real-scheduler-shaped log to begin with) — but this is the same shape
    of risk tripwire 17 exists to catch, one step removed: the gate G1 test *would* flip from
    pass to fail the moment its input log is regenerated against the real scheduler (which
    `fixtures/code_pipeline/golden_findings.json`'s own `"status"` field says is already
    overdue), and nothing in §9/§10 anticipates or flags that dependency.

---

## 6. HONESTY AUDIT

- **`causality.py`'s module docstring (lines 61-65)** states: *"a race detector re-deriving the
  one property its entire output depends on, rather than trusting an upstream writer, is the more
  defensible default ... `Event.vclock` and this module's own output can be, and in
  `tests/analysis/race/test_causality.py` are, cross-checked against the real golden fixture logs
  and found to agree exactly."* The word "real" here is doing a lot of unearned work: the golden
  fixture logs are, by `fixtures/_harness.py`'s and `ADR-001`'s own explicit and repeated
  admission, a **provisional, simplified stand-in** for what the real scheduler produces, in
  exactly the dimension (`causal_parents` fidelity) this module's entire design depends on. This
  is not a fabricated number — every count and pass/fail claim I checked was real — but it is an
  overclaim of *what the measurement demonstrates*. "Agrees exactly with the golden fixture logs"
  is true and checkable; "agrees exactly with the real stamped vclock" (the test's own name:
  `test_build_causality_matches_the_real_stamped_vclock_exactly`) conflates the fixture harness
  with the real runtime, and that conflation is precisely where the headline bug hides.
- **`docs/race-detection.md`'s "Honest recall and coverage statement"** lists six specific things
  not built, in the spirit AGENTS.md §8 asks for. It does not list the one gap that matters most:
  that the algorithm's foundational assumption (`causal_parents` means "declared happens-after")
  is false against the one other component in this codebase (`runtime/scheduler.py`) that
  actually produces `causal_parents` values the way a real deployment would. This omission is
  more consequential than any of the six it does list, and the information needed to find it
  (`runtime/scheduler.py`'s own comments distinguishing "declared" causal parents from
  `Stamp.causal_parents`, written by an earlier prompt for a different purpose) was already
  sitting in the repository.
- **`CONTEXT.md`'s P12 paragraph (§7, and the mirrored §13 row)** states "proven equivalent to
  §14.2's five named rules" for `build_causality`. The equivalence proof given is genuinely a
  proof — *if* `causal_parents` means what the five PRD rules assume it means. The paragraph does
  not qualify this, and elsewhere states the module is I3-pure and "must not trust a runtime's
  stamped `Event.vclock` field blindly" as though this were a strictly more cautious design
  choice than trusting `Event.vclock` — it is not more cautious in the dimension that actually
  matters, because `causal_parents` is *also* runtime-stamped, and stamped with an artifact the
  vclock computation itself was specifically written to exclude (`_compute_vclock` uses declared
  `causes` only; `_causal_parents` does not).
- **The "153 events, zero mismatches" figure** is real and independently reproducible (I recomputed
  it: 48+64+41=153). **The 78-test, 2030-total-suite, mypy/ruff/lint-imports/determinism-hygiene/
  bench-marker/fixture-evidence-clean claims** are all real — I ran every one of them live and got
  identical results to what `CONTEXT.md` and the module docstrings claim. Nothing in the
  mechanical CI-style checks was fabricated or stubbed.
- **`op2-audit-p09.md`**, cited in `CONTEXT.md`'s P09 paragraph as containing "34 mutation-tested
  findings," does not exist anywhere in this tree. This predates P12 and is not a P12 defect, but
  it is a data point about this ledger's citation reliability that a handoff reader should know:
  at least one specific, named, quoted-finding-count artifact citation in `CONTEXT.md` cannot be
  verified because the file does not exist.

---

## 7. HANDOFF READINESS

A different AI resuming from `CONTEXT.md` + the PRD alone, with no access to this audit, would
almost certainly get the following wrong:

1. **It would believe gate G1 is soundly met.** `CONTEXT.md` §6 states G1 "self-reported PASS"
   with a pasted, real terminal transcript. The transcript is real and the command really does
   pass — but it passes against a golden log that the ledger's *own* fixture metadata
   (`golden_findings.json`'s `"status"` field, ADR-001) already says is overdue for
   regeneration now that P06/P07 are built. A resuming agent reading only the pasted G1 output
   would have no reason to go check that dependency.
2. **It would not know to distrust `Event.causal_parents` as a source of "genuine happens-after."**
   This distinction is documented in `runtime/scheduler.py` and `runtime/faults/taint.py` (both
   written for P09, a different purpose), but nowhere in `docs/race-detection.md` or `CONTEXT.md`'s
   P12 paragraph. A resuming agent asked to extend `causality.py` (e.g., to wire in a real
   `RunHost`/scheduler at P17) would have every reason to believe the existing design is sound and
   to build on top of it rather than around it.
3. **It would assume the fixture golden logs are a faithful stand-in for real runtime output**
   because P12's own tests treat them that way (`test_build_causality_matches_the_real_stamped_
   vclock_exactly`) without the qualifying context that `fixtures/_harness.py` is explicitly,
   self-declaredly simplified in exactly this dimension.
4. **The undocumented assumption, stated plainly:** *"`Event.causal_parents`, as persisted by the
   production runtime, equals the set of events this event genuinely happens-after."* This is
   false. It is false by the runtime's own design (the log-continuity fallback exists on purpose,
   for the hash chain), and the falseness is currently invisible because nothing in this codebase
   has yet run the SDK against the real scheduler end to end (`sdk.generic.RunHost` is itself
   still unbuilt, per `CONTEXT.md` §5 row 6/P06's own paragraph) — so the discrepancy has had no
   chance to surface as a failing gate yet. It will, the first time `code_pipeline` is replayed
   through a real `RunHost` + `Scheduler` instead of `fixtures/_harness.py`.

---

## VERDICT: **FAIL**

This is not a borderline "PASS WITH NOTES." The module's own stated purpose — detect a real
concurrency race and not merely a fixture-shaped stand-in for one — fails on exactly the
scenario the PRD's own worked example and gate G1 are built around, the moment the input log is
shaped the way the one other already-built component in this codebase (`runtime/scheduler.py`)
actually produces it. Every mechanical gate (tests, mypy, ruff, lint-imports, determinism
hygiene, bench markers, fixture evidence) is genuinely green, and the guard logic (G1–G4) is
well-designed and well-tested *given a correct causality graph* — but the causality graph itself
is built on a field whose real-world semantics the implementation misreads, and that misreading
is invisible to all 78 tests because every one of them constructs its own `causal_parents` by
hand in the one shape (empty for unsynchronised state ops) that avoids exposing the bug.

**Minimum fixes, in priority order:**

1. **Fix `build_causality`'s source of truth.** Either (a) trust the persisted `Event.vclock`
   field directly (it is correctly computed from *declared* `causes` only by
   `Scheduler._compute_vclock`, and does not carry the log-continuity fallback), or (b) if the
   "never trust an upstream-stamped value blindly" design goal is to be kept, thread a genuinely
   declared-only causal-parents signal through to `analysis/` — which does not exist on the
   persisted `Event` today, so this option requires either a schema change (event schema is
   frozen — needs an ADR) or accepting that `analysis/` cannot re-derive this from a sealed log
   alone and must trust `Event.vclock`. Whichever direction is chosen, it needs an explicit ADR,
   because it is a genuine, load-bearing design decision the PRD does not resolve for this
   specific ambiguity (this is `AGENTS.md` §3's uncertainty protocol territory — it should have
   been a `⚠️ BLOCKED` during the original build, not a silent choice).
2. **Regenerate the golden fixture logs against the real `runtime/scheduler.py` + a `RunHost`**,
   per `ADR-001`'s and `golden_findings.json`'s own stated condition ("regenerate at P07"), which
   is now satisfied (P06 and P07 are both `BUILT`). Until this happens, gate G1's own pasted
   "PASS" is evidence about a log shape that will not exist once the system is used for real.
3. **Add at least one test per critical suite (`test_causality.py`, `test_gate_g1.py`,
   `test_true_positive_matrix.py`) that constructs `causal_parents` the way
   `Scheduler._causal_parents` actually does** — i.e., the linear fallback for any event with no
   declared `causes` — rather than universally hand-setting it to the PRD-ideal empty shape. This
   is the single change that would have caught the headline bug before it shipped.
4. Once (1)–(3) land, this module is owed a **second, independent OP-2** before `VERIFIED` — the
   standing pattern every other module in this project already follows, and this one is no
   exception.

Everything else audited here — the guard implementations, the classification logic, the dedup
rule, the minimal-repro scope declaration (D-53), the k=2 harness's ADR-002 compliance, the
mechanical CI-style gates — is solid and would likely survive a second audit unchanged. The fix
above is narrow in code surface (one function's source field) but load-bearing for the entire
module's claim to work.
