# OP-2 audit — P20 (`runtime`): "D-62 is not caused by fan-out — measured, and the framing corrected"

**Reviewer:** independent (did not write this code). **Under review:** commit `5a17eb5`, the tip
of `main`, working tree clean. **Scope note:** no prompt text with a `DELIVERABLES` list exists
for "P20" anywhere in `CONTEXT.md` — there is no §5 roadmap row for it, and it is not an `OP-1`
(replan), `OP-2` (audit), or `OP-3` (repair) in `CONTEXT.md` §0's own taxonomy. This audit treats
the diff itself as the deliverable and the commit message as the closest thing to a spec. That
absence is itself a finding (§3, §7).

Diff under review: `commit-step1.sh` (new), `d62-design.md` (edited), `tests/integration/runtime/__init__.py`
(new), `tests/integration/runtime/test_d62_suspension_contract.py` (new). 4 files, +396/-35.
`CONTEXT.md`, `Dockerfile` and every other product file are untouched.

---

## 1. SPEC CONFORMANCE

No PRD FR is claimed as implemented, and none should be — this is declared, correctly, as "an
experiment, not a regression suite" that "changes no product behaviour." Judged against that
self-declared scope:

- **Delivers what it claims:** a four-probe test ladder that locates the deadlock boundary, and
  a design-doc correction. Both exist, both are real (verified below).
- **PRD §3480's own scheduler API table names exactly `Scheduler.run()` and `Scheduler.register(task)`
  — not `spawn()`.** `spawn()` exists in code (`runtime/scheduler.py:752`) but is not a PRD-named
  surface. `d62-design.md` §1 already flags widening `sdk.generic.Scheduler` as a public-interface
  change needing an ADR before implementation (`AGENTS.md` §3) — correctly caught, not a defect,
  but worth stating plainly: closing D-62 by adding `spawn` to the SDK Protocol is not something
  the PRD asks for by name, it is an inference from `CONTEXT.md`'s own prior (and, per this diff,
  partly wrong) framing.
- **Process nonconformance:** `AGENTS.md` §7 requires every response to end with a `CONTEXT LEDGER
  PATCH` — "a copy-paste-ready diff for `CONTEXT.md`." No such patch was applied. `CONTEXT.md`
  contains zero occurrences of the string "P20" (checked directly). Whatever this prompt's process
  pedigree, the ledger update that `AGENTS.md` makes mandatory for every response did not land.

## 2. INVARIANT CHECK

Only I1 is in play; the rest are not touched by a design-doc edit and two new test-only files.

| Invariant | Status | Mechanism |
|---|---|---|
| I1 (determinism) | **Correctly out of scope, not violated** | Runs with `strict_determinism=False` deliberately, and says why: `strict` also gates `_patch_time`/`_patch_thread_spawn`, so a clock read inside Pregel would raise `DeterminismLeakError` before the scheduler loop is even reached, "impersonating" the failure under test. This is the right call and it's justified in the docstring, not silently done. |
| I1 — production-posture caveat | **Not stated, and it matters** | I reproduced the decisive probe myself (see §6) and got a `NondeterminismLeakWarning [E-SCHED-004]`: LangGraph dispatches a **synchronous** node via `run_in_executor`, spawning a real OS thread. Under the project's actual default (`strict=True`), that thread spawn would raise `DeterminationLeakError` *before* `DeadlockError` is ever reached. The real fixtures' nodes are all `async def` (`fixtures/code_pipeline/graph.py` — `planner`, `coder`, `reviewer`, `tester`), not sync, so this exact confound doesn't hit them — but the diff never checks or states that the real fixtures avoid it. I re-ran the decisive probe with a genuinely `async def` node (matching the real fixtures' shape) and it deadlocks identically (§6), so the qualitative conclusion holds — but that check isn't in the diff, and the shipped test's own docstring claims fidelity to production ("driven … exactly as production does it") while using a node shape production doesn't use. |
| I13 / I3 / others | N/A | No `analysis/`, `sdk/`, or model-facing code touched. |

## 3. SCOPE VIOLATIONS

- No refactor of prior prompts' code — confirmed; zero lines of `src/agentdx/` changed.
- `commit-step1.sh` (a script that runs checks, then git-commits with a baked-in message) matches
  an established repo pattern (`commit-p19.sh`, `commit-item5{,b,c,d}.sh`, `verify-p19-repair.sh`
  already exist) — not a new practice, not flagged.
- `tests/integration/runtime/test_d62_suspension_contract.py` imports `build_scheduler` from
  `tests/unit/runtime/conftest.py` rather than duplicating it — correct reuse.
- **The one real gap:** with no `DELIVERABLES` list to check the diff against (§0 above), "did
  this stay in scope" can only be judged against the commit's own narrative, which is
  self-consistent — but that also means nothing external constrained it, which is exactly the
  condition `AGENTS.md` §2 exists to prevent for every other prompt in this ledger.

## 4. THE TEST-QUALITY QUESTION

**Test A — `test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all` (the decisive
probe).** As shipped, its node (`_only_node`) is a **plain `def`**, not `async def`. LangGraph
routes a sync node through `run_in_executor` — a thread-handoff, which is *a* way to need "the
loop to run another task," but not the *only* way, and not the way the real fixtures do it.
**Bug this would miss:** someone "fixes" D-62 narrowly — e.g. special-cases awaiting on a
`concurrent.futures.Future`/executor completion, since that's the literal shape this test
exercises — and ships it. This test goes green. A real multi-node fixture with concurrent
`async def` nodes (no executor thread anywhere) could still deadlock via a different suspension
path, and nothing here would catch it. **What would catch it:** the test already ships Control 3
(`test_a_suspension_needing_another_task_deadlocks`, pure `asyncio.Event`/`Task`, no threads),
which does generalize past the executor-thread case — but the *decisive* LangGraph test itself
should use an `async def` node (I confirmed empirically it still deadlocks, §6) so the "exactly as
production does it" claim in its own docstring is actually true, and so a fix narrowly scoped to
threads-only would fail it.

**Test B — `test_a_suspension_needing_another_task_deadlocks` (Control 3).** This is the one
that will get load-bearing the moment someone builds Option D ("yield to the real loop and
re-check; if the loop makes progress, continue"). It has exactly **one** intermediary task (the
`_setter`, one hop away). **Bug this would miss:** an Option D implementation that grants exactly
one extra real-loop turn and re-checks once — correct for this test's one-hop case — but not
genuinely iterative. A **two-hop** chain (task A awaits an Event set by task B, which itself only
fires after being triggered by task C) would still deadlock under that fix, and this suite has no
test for it. **What would catch it:** a chained-dependency variant of Control 3 with ≥2
intermediary tasks, asserting completion (once a fix exists) rather than deadlock — this is the
regression test the *next* prompt owes, and it isn't written yet because Option D isn't chosen
yet, but it's worth flagging now since `d62-design.md` §3a already anticipates Option D as a live
candidate.

**Credit where due:** the *retraction* of the original "one tick" control is itself a good
catch — the file explains precisely why `ensure_future` + `Event.wait()` could never have passed
as a "zero-tick" control, and removes the unmeasured claim rather than quietly keeping it. That's
the kind of thing this project's own tripwire list rewards.

## 5. DRIFT TRIPWIRES (`CONTEXT.md` §11, walked item by item)

Items 1–14, 16–19: **not fired** — no test was weakened to pass (the replacement control is
strictly more rigorous than the one it retracts), no banned non-determinism source appears under
`src/agentdx/`, no `analysis/` import violation, no evidence-free finding, no magic number, no
schema change, no published statistic without a marker, no frontend touched, no out-of-scope
feature, no §8/§9 row edited or removed (confirmed — the D-62 row at line 313 is untouched, its
outdated content notwithstanding — see §6), no acceptance-gate test involved.

**Item 15 fires: "A workflow on `main` is red, or has been red across more than one run."**
`pyproject.toml`'s `[tool.pytest.ini_options]` excludes only `-m 'not acceptance'` from default
collection. The new test file carries no marker. `tests/integration/runtime/test_d62_suspension_contract.py::test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all`
is written to **fail by design** as long as D-62 is open (confirmed live, §6) — meaning `pytest`,
`just test`, and `just ci` all go red the moment this lands on `main`, with no expiry. The
project's own `pyproject.toml`, four lines above the `addopts` line, already documents the exact
failure mode and its fix for a different suite: the `acceptance` marker exists specifically
because "mixing them into the default … loop would make routine development red for reasons no
single commit can fix (`AGENTS.md` §8's alarm-fatigue concern)." This commit reproduces precisely
that condition one day after `CONTEXT.md`'s own 2026-09-01 session log records "the first
end-to-end green `just ci` in the project's history," without using the mitigation the project
had already built for this exact scenario. `commit-step1.sh` itself treats the resulting non-zero
`pytest` exit as expected and proceeds to commit anyway — the failure is not accidental, but it
is also not marked, so nothing distinguishes it from a real regression to whoever next runs
`just ci` or watches the Actions tab.

**Not on the numbered list, but the same species of gap:** nothing in §11 catches "the state of
record contradicts the newest evidence in the repo, and no ledger row exists to say so." Tripwire
14 was added for "too little" implementation; this is "too little" *documentation* of a
correction the diff's own commit message claims was made. Worth naming as a candidate future
tripwire, the way 14 was added after D-16 exposed a gap in 11's coverage.

## 6. HONESTY AUDIT

**Verified, independently, and it holds up.** I could not obtain the claimed environment
(Darwin/arm64, CPython 3.12.2 — this sandbox has no path to a real 3.12, the same
`python-build-standalone`/GitHub-releases network block this ledger has documented since P02/P19).
I built a substitute (Linux/aarch64, CPython 3.10.12, with `tomllib`/`enum.StrEnum` shims of the
same kind prior sessions in this ledger used) and ran the actual committed test file:

```
tests/integration/runtime/test_d62_suspension_contract.py::test_a_root_that_never_suspends_completes PASSED
tests/integration/runtime/test_d62_suspension_contract.py::test_a_root_that_awaits_an_already_resolved_future_completes PASSED
tests/integration/runtime/test_d62_suspension_contract.py::test_a_suspension_needing_another_task_deadlocks PASSED
tests/integration/runtime/test_d62_suspension_contract.py::test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all FAILED
1 failed, 3 passed
```

— the same qualitative pattern claimed in the commit (3 controls pass, the decisive probe fails
with "HYPOTHESIS CONFIRMED"), and the failure text matches the claimed real-world signature
(`E-SCHED-003`, empty `wait_reason`) byte-for-shape. I also spot-checked every source line
citation in `d62-design.md` (`scheduler.py:752`, `sdk/generic.py:261-271`,
`sdk/langgraph.py:813`/`:643`, `providers/openai_compatible.py:299`/`:308`, and the "zero `.spawn(`
calls anywhere in `sdk/`" claim) against the actual files — **all of them are exactly right**,
several to the exact line number. This is genuinely careful work.

**Where the framing overclaims:** the commit title is "D-62 is not caused by fan-out — measured,
**and the framing corrected**." Only `d62-design.md`'s framing was corrected. `CONTEXT.md` §9's
D-62 row still asserts, as fact, that fan-out causes the deadlock (line 313, untouched). The
`Dockerfile` header's own "CORRECTION" note (added in an earlier session) still says "One test
settles it; it has not been run" — which is now false; the test has been run, and this diff's own
`commit-step1.sh` trailing output *acknowledges* this ("CONTEXT.md §7/§9's fan-out framing and the
Dockerfile header both still carry the [uncorrected] claim") without fixing either. So: two of
the three documents the commit message says were "wrong" are still wrong after the commit. The
disclosure exists (in a shell script's echo statement, not in the commit message or `CONTEXT.md`
itself), which is better than silence, but the headline claim is broader than what shipped.

**Also found:** `d62-design.md` §6 ("Recommended sequence") still reads "1. Run the §3
experiment. One test, and it decides the framing" — step one of the *forward-looking* plan
literally instructs the reader to do something §3, four sections earlier in the same file, says
is already done. This section wasn't part of the diff and is now self-contradictory on a fresh
read.

**Nothing found that resembles a fabricated result or a stub presented as done.** The document is
explicit throughout about what is design-only vs. measured vs. retracted.

## 7. HANDOFF READINESS

A different AI resuming with only `CONTEXT.md` and the PRD — the project's own mandated
opening ritual (`AGENTS.md` §1, `CONTEXT.md` §0) — would get this wrong in three concrete ways:

1. **It would read the wrong cause.** `CONTEXT.md` §9's D-62 row still says fan-out causes the
   deadlock. §7's "Current position" is frozen at 2026-08-27 (P18/19) and never mentions this
   session, the 2026-09-01 verification session, or P20. Nothing in `CONTEXT.md` points at
   `d62-design.md` §3a (Option D) at all. The only way to learn any of this happened is `git log`
   — which is not part of the mandated reading list.
2. **It would under-scope Option D.** `_run_once` (CPython's event-loop step) processes a fixed
   snapshot of the ready queue per pass; a task scheduled *during* the current pass (like the
   `_setter` task in Control 3) waits for the *next* pass. That's the actual reason "one tick" was
   never enough, and it means a naive Option D — "yield once more and re-check" — will pass every
   test in this file (all single-hop) while still deadlocking on any chain of ≥2 dependent
   suspensions. `d62-design.md` doesn't say this; it stops at "requires the event loop to run
   another task." Whoever builds Option D next needs to know it's a *quiesce-until-idle* problem,
   not a *one-more-turn* problem — this document doesn't tell them that, and Test B's own gap
   (§4) means they won't be caught if they get it wrong.
3. **It would not know CI is red for a reason that isn't a regression.** No marker, no note in
   `CONTEXT.md`, nothing in the CI workflow. The next person to see red `just ci` has to
   independently discover this test is *supposed* to be red right now.

---

## VERDICT: **FAIL**

Not on the science — the measurement is real, independently reproduced here, and the retraction
of the broken control is a genuinely good practice this ledger should want more of. It fails on
the same grounds this ledger has repaired other prompts for before: an unmarked red test landed
in the default suite with an already-established mitigation available and unused, and the one
document this entire project treats as the source of truth was left asserting the specific claim
this diff just spent an experiment disproving.

**Minimum fixes, in priority order:**

1. **Stop the CI bleed.** Add a marker (e.g. `experiment`, parallel to the existing `acceptance`
   pattern) excluding this file from default `pytest`/`just test`/`just ci` collection, or an
   explicit `@pytest.mark.xfail(reason=..., strict=True)` on the decisive test — either preserves
   "a red test whose failure is the finding" without silently reintroducing a red `main`.
2. **Patch `CONTEXT.md`.** Append (not edit) a corrected D-62 §9 row or a new §10 ruling stating
   the fan-out framing is retracted, per `d62-design.md` §3; update §7's "Current position" and
   add a §13 row for this session. This is the `AGENTS.md` §7 ledger-patch obligation that's
   currently outstanding.
3. **Fix the `Dockerfile` header** and the `d62-design.md` §6 self-contradiction — both are
   one-paragraph edits, already fully specified by this diff's own content.
4. **Swap the decisive test's node to `async def`** (confirmed still deadlocks, §6) so its "exactly
   as production does it" claim is literally true, closing the executor-thread confound.
5. Before Option D is built: add the multi-hop chained-suspension variant of Control 3 described
   in §4, so a fix that grants only one extra real-loop turn is caught rather than shipped green.
