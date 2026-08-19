# OP-3 STABILISATION PLAN — P09 `runtime/faults/` + chaos safety

**Do not begin repairing until this plan is approved.** Per the STOP CONDITIONS below, four items require a human decision before any repair touches them; the rest are ready to execute once approved.

---

## SITUATION

An independent OP-2 audit of P09 (`runtime/faults/` + chaos safety, full report: `op2-audit-p09.md`) returned **FAIL**. It found: nine-plus PRD requirements in P09's own scope that were neither implemented nor declared as gaps (CONTEXT.md §11 tripwire 14 fires); two structurally load-bearing tests (`safety.py`'s "enforced at two layers" claim, and the determinism-with-faults gate) pass unchanged when the exact thing they claim to guard is deliberately broken; three false "pasted output" citations and a wrong test count in CONTEXT.md/`docs/chaos-safety.md`; a magic-number/duplicated-config-source tripwire (§11 #5); and one banned `pass` stub. The build's core mechanism (fault taint, the D-44 causal-parent fix, scope discipline) is sound — this is a repair, not a rewrite.

---

## 1. GROUND TRUTH — what P09 is supposed to do

From PRD §9.4, §12, §13 and CONTEXT.md §2/§11, restated without reference to the code:

- **§9.4 (taint):** every event caused by a fault carries `fault_id`, downstream events inherit it through causal parents, ties resolve to the *earliest* contributing fault, and where multiple faults contribute the full set is recoverable via `payload.fault_ids` — not just the earliest.
- **§12.1–12.5 (fault catalogue):** each MVP fault type (`latency`, `agent_crash`, `message_drop`, `tool_failure`) carries a normative **Safety** row that is a hard behavioural constraint, not documentation — e.g. `agent_crash` cannot kill the last live agent unless the scenario explicitly opts in; `message_drop` cannot drop a `run_end` control message; `latency` is bounded by `max_virtual_duration`. Every armed/fired fault is logged (`fault_injected`/`fault_effect`) and a `fault_summary` lands in run metadata.
- **§13.3–13.4 (authorization):** chaos is fixture-only by default, and blast-radius authorization is enforced **at two layers** — arm-time (scenario load) and fire-time (immediately before the fault executes) — specifically so that a target leaving the blast radius between arm and fire cannot be exploited. The invariant table's own claimed mechanism is "runtime re-check," not "code-review discipline."
- **§13.5 (steady state):** an hypothesis metric that was declared but never measured during the run is a violation, not a pass-by-default.
- **§13.6 (abort guards):** on any guard trip, the injector disarms, in-flight tasks are cancelled, the log is sealed with `run_end.status = aborted_guard`, and the partial log stays analysable.
- **§10.5/§10.7 (determinism):** the canonical log is byte-identical across replays for *any* MVP fault, including probability-triggered ones — not just the one deterministic-by-construction trigger kind.
- **§24.3 (module map):** `runtime.faults` depends on `scheduler` and `events` only.
- **AGENTS.md §4 / CONTEXT.md §11 #5:** no magic numbers; one source of truth for a given default.
- **AGENTS.md §6:** a claim that output was "pasted" means it is actually present, verbatim, in the named file.

## 2. ACTUAL STATE — what the code does, undecorated

- `CrashInjector._crash` reads `params["recoverable"]`, ignores the co-located `params["allow_total_failure"]`. Nothing stops crashing the last live agent.
- `TransportFaultInjector.decide_drop(*, edge, virtual_ts_ms, message_count)` has no parameter that identifies message *type*; a `run_end` control message cannot be distinguished from any other, so it cannot be protected.
- `TransportFaultInjector.decide_latency`'s `degrade` pattern returns `delay_ms * (fire_count + 1)` uncapped; nothing reads `AbortGuardMonitor`'s `max_virtual_duration_ms` from inside it.
- `registry.summary()` computes `fault_summary`/`fault_not_triggered`; no caller writes it anywhere a run's metadata is persisted.
- `compute_causal_taint` and `FaultTaintTracker.resolve` both keep only `min(candidates)` and discard the rest; `payload.fault_ids` does not exist anywhere in the codebase.
- `safety.reauthorize` is called from all three execution modules, but every call site can be deleted (4 lines, 4 files) with the full 94-test `tests/unit/faults`+`tests/integration/faults` suite staying green. `BlastRadius.contains` is untested for `TOOL`, `EDGE`, `PROVIDER`.
- On a tripped `AbortGuardMonitor` guard: no `run_end` event is written, the `EventWriter` is not sealed, the injector is not disarmed, in-flight tasks are not cancelled, and `PAYLOAD_SCHEMAS[RUN_END]['status'].enum` has no `aborted_guard` member to write even if the rest were fixed.
- `test_determinism_with_faults.py`'s 100/100 gate arms exactly one `AT_VIRTUAL_TS`-triggered `agent_crash` — the one MVP fault with no randomness in its firing decision. `FaultRandomStream.next_permille` can be made to raise unconditionally and both this gate and G4 stay green. `triggers.py`'s permille modulus can be changed from `% 1000` to `% 100` — inverting every probability-triggered fault's real fire rate — with all 94 tests green.
- `runtime/faults/{transport,dependency,registry,triggers}.py` import `agentdx.scenario.schema`; CONTEXT.md §4's layer table and PRD §24.3 both say `runtime` depends on `events`/`store` only. `.importlinter`'s contract is a forbidden-list, so this passes the linter without being an approved edge.
- `AbortGuardMonitor.observe_step` receives `wall_time()`-derived `wall_elapsed_ms` from `process.py:263,268` and its return value can raise `AbortGuardTripped`, branching run control flow off a real-clock read — a use AGENTS.md §4.1's four sanctioned exemptions do not cover.
- `triggers.py`'s `REPEATING_TRIGGER_KINDS` ruling (PRD gap: no scenario field for `fault.repeating`) is resolved only in a module docstring, not as a CONTEXT.md §10 `C-n` row.
- `safety.py:167`'s `DEFAULT_GUARD_EVAL_STEP_INTERVAL = 100` and six inline `registry.param_int(...)` defaults live beside, not inside, `scenario/loader.resolve_defaults` / `agentdx.toml`.
- `process.py:279-288` contains a literal `if <condition>: pass` block.
- CONTEXT.md §5/§7/§13 and `docs/chaos-safety.md:104-105` claim G4 and safety-suite output is "pasted" in `docs/chaos-safety.md`; the file's "Gates and how to reproduce them" section contains commands only, no output. The same documents state `tests/unit/faults/` has "91 tests"; the actual count is 79.
- `docs/chaos-safety.md` has two internal TOC links pointing at slugs that don't match the headings' actual generated anchors.

---

## 3. THE DELTA TABLE

| # | Expected (PRD/CONTEXT.md) | Actual | Where it entered | Severity |
|---|---|---|---|---|
| D1 | §12.2 `agent_crash`: refuse to crash the last live agent unless `allow_total_failure: true` | Parameter reaches `FaultDecl.params`, never read | P09 | **High** — safety-row bypass, explicitly flagged forward by CONTEXT.md §14 for P09 |
| D2 | §12.2 `message_drop`: never drop a `run_end` control message | `decide_drop` has no message-identity input; unexpressible | P09 | High |
| D3 | §12.2 `latency`: bounded by `max_virtual_duration` | `degrade` pattern uncapped | P09 | Medium |
| D4 | §13.3–13.4: blast radius enforced at two layers, fire-time re-check structural | `reauthorize` calls are real but zero-regression-covered; deletable with all tests green | P09 (test-quality gap, not a functional defect today) | **High** — no safety net against future regression |
| D5 | §13.6: on guard trip — disarm, cancel in-flight, seal writer, `run_end.status = aborted_guard` | None of the four happens; `run_end.status` enum has no `aborted_guard` member at all | Behaviour: P09. Missing enum member: **P02** (`events/schema.py`), inherited/exposed by P09 | **High** |
| D6 | §9.4: `payload.fault_ids` holds the full contributing-fault set | Field does not exist; only earliest (`fault_id`) is kept, in two places | P09 | Medium |
| D7 | §12.5: `fault_summary` lands in run metadata | Computed (`registry.summary()`), never persisted anywhere | P09 | Medium |
| D8 | §10.5/§10.7: I1 holds for every MVP fault | Only demonstrated for the one non-probabilistic fault; RNG stream provably never drawn in the gate; modulus bug (10x) undetected | P09 | **High** — the exact invariant the gate exists to prove is unmeasured for 3 of 4 MVP faults |
| D9 | §24.3 / CONTEXT.md §4: `runtime` depends on `events`/`store` only | `runtime/faults/*` imports `scenario.schema`, undeclared | P09 | Medium |
| D10 | AGENTS.md §4.1: real-clock reads confined to 4 sanctioned exemptions | `wall_time()` read branches run-aborting control flow in `process.py`, a 5th, undeclared use | P09 | Medium |
| D11 | AGENTS.md §3: a resolved PRD gap gets a CONTEXT.md §10 `C-n` row | `fault.repeating` ruling exists only in a `triggers.py` docstring | P09 | Low |
| D12 | AGENTS.md §4: no magic numbers, one config source of truth | `DEFAULT_GUARD_EVAL_STEP_INTERVAL` and 6 fault-param defaults live inline, duplicating `scenario/loader.resolve_defaults`'s role (tripwire §11 #5) | P09 | Low |
| D13 | AGENTS.md §2: no stub/placeholder logic shipped as done | `process.py:279-288` literal `pass` block | P09 | Low |
| D14 | AGENTS.md §6 / Rule E1: a claim that output is "pasted" is true | 3 "pasted" citations in CONTEXT.md/`docs/chaos-safety.md` point at a section containing no output; test count stated as 91, actual 79 | P09 | Low (honesty, not functional) — but same class of defect as the already-twice-corrected C-11/C-13 |
| D15 | Internal doc consistency | Two TOC anchors in `docs/chaos-safety.md` don't resolve | P09 | Trivial |
| D16 | Two independent "earliest fault wins" implementations should agree on edge-case behaviour | `compute_causal_taint`'s unknown-fault default and `FaultTaintTracker.resolve`'s `1 << 62` sentinel diverge; no PRD text specifies the unknown-fault case | P09 | Low |

---

## 4. ROOT CAUSE, per row

| # | Cause | Class |
|---|---|---|
| D1, D2, D3, D6, D7 | Correct, unambiguous spec; implementation incomplete (a). Data was already plumbed in every case (D1 confirmed live: `FaultDecl.params` carries the flag already). | **(a)** — repair directly, but D6/D7 first require the schema question below |
| D4 | Correct spec, correct *code*, but the docstring's claim of structural enforcement is false and no test exists to make it true. Not a functional bug today; a missing safety net. | **(a)**-adjacent (test-quality), repair directly |
| D5 | Behavioural part (disarm/cancel/seal) — **(a)**, repair directly. The `run_end.status = aborted_guard` part — the enum member the PRD's own requirement needs does not exist in the P02 schema. This is **(c)**, a genuine spec gap silently filled (by mapping to `FAILED` instead) | **STOP — (c), needs a decision** |
| D8 | Correct spec (I1 must hold for the whole fault engine), test coverage narrower than the invariant it claims to prove. No spec ambiguity. | **(a)**, repair directly |
| D9 | The import may be a reasonable, even necessary, dependency (fault triggers need `TargetKind`/`TriggerKind`) — but it was never logged, so it's neither ratified nor rejected. | **STOP — (d), needs a decision** |
| D10 | PRD §13.6 *requires* a wall-clock-bounded guard, which by definition needs a real-clock read influencing control flow — but AGENTS.md §4.1's four sanctioned exemptions don't cover this case. The rule and the requirement are in tension. | **STOP — (e), needs a decision** |
| D11 | A reasonable ruling, filed in the wrong place (docstring instead of ledger). | **(d)** technically, but the ruling itself isn't in dispute — **flagging for a one-line ratification, not a design decision** |
| D12, D13, D15 | Straightforward rule violations (AGENTS.md §4/§2, doc hygiene). No spec ambiguity. | **(a)**, repair directly |
| D14 | Documentation/evidence-discipline defect, not a spec-vs-code mismatch — doesn't map cleanly onto (a)-(e). Same shape as the already-established C-11/C-13 correction pattern (append a correction, don't silently edit). | Repair directly, using the established pattern |
| D16 | Neither implementation is "wrong" against the PRD, because the PRD doesn't address the unknown-fault case. | **(c)**, but low severity — bundling into the decision batch below with a recommendation, not blocking the rest of the plan |

---

## STOP — four decisions needed before repair starts

Per the procedure's stop conditions ("changing the event schema," "resolving a case (c), (d) or (e) root cause"), these four are pulled out of the ordered repair plan and need your call:

**Q1 (D5, blocks part of item 1 below).** `run_end.status` has no `aborted_guard` enum member.
- **A) Add `aborted_guard` to `PAYLOAD_SCHEMAS[RUN_END]['status']`'s enum now** — an additive schema change (new enum member, no field removed/retyped), requires a `schema_version` bump per tripwire #6 and touches P02's file a second time (D-43/D-44 already did once, for a different reason).
- **B) Keep mapping guard-trip to the existing `FAILED` status for now**, fix only the behavioural gaps (disarm/cancel/seal), and log this as a formally deferred, declared gap for a later prompt to close alongside other schema work.
- My recommendation: **(A)** — it's additive, low-risk, and PRD §13.6 is explicit that this is a distinguishable outcome; leaving it mapped to `FAILED` means an analysis layer built later can never tell an abort-guard trip apart from a genuine failure, which compounds the debt rather than isolating it.

**Q2 (D9).** `runtime/faults/*` imports `agentdx.scenario.schema` for `TargetKind`/`TriggerKind`, an undeclared new layer edge.
- **A) Ratify it** — add an ADR, update CONTEXT.md §4's layer table, and add a `runtime→scenario` entry to `.importlinter`'s allowed set so it's an enforced permission, not an accidental gap.
- **B) Remove the dependency** — move `TargetKind`/`TriggerKind` (or a minimal shared subset) into `events` or a new low-level shared module both `scenario` and `runtime` can import, restoring the original layering.
- My recommendation: **(A)**. `scenario` is a declarative config surface, not a runtime participant (`.importlinter`'s own contract descriptions call it "a declarative surface with no layer dependencies" the other direction) — importing its type *definitions* into the runtime that executes against them isn't a layering violation in spirit, just in an un-updated diagram. (B) is more invasive for no behavioural gain.

**Q3 (D10).** `wall_time()` feeds `AbortGuardMonitor`, whose return value can abort a run — a real-clock read outside AGENTS.md §4.1's 4 sanctioned exemptions.
- **A) Add a 5th sanctioned exemption** for wall-duration guard evaluation specifically, scoped narrowly (only `max_wall_duration_s` checks, nothing else), documented in AGENTS.md §4.1 same as the other four.
- **B) Redesign** so the wall-clock sample is recorded as a volatile-field write (already-sanctioned category 3) on each step, and the guard trip is evaluated by a **separate**, explicitly non-deterministic watchdog outside the replayable log, rather than branching the scheduler's own control flow.
- My recommendation: **(A)**. PRD §13.6 explicitly wants a wall-duration guard to actually abort the run, not just annotate it after the fact; (B) would silently weaken that requirement. The exemption is narrow and precedented (the volatile-field-writer category already carves out similar exceptions).

**Q4 (D16, low-stakes, bundled here rather than blocking).** `compute_causal_taint` and `FaultTaintTracker.resolve` disagree on the tie-break default for an unrecognized `fault_id`.
- Recommend aligning both to the `FaultTaintTracker`'s `1 << 62` "never wins" sentinel, and adding one test asserting the two implementations agree — no PRD text to violate either way, this is just closing an inconsistency. Flagging for a one-line "yes, do that" rather than real alternatives.

**D11 ratification (not really contested, flagging per the letter of the stop rule):** log the existing `REPEATING_TRIGGER_KINDS` ruling as a new CONTEXT.md §10 `C-n` row, verbatim from `triggers.py`'s docstring. Say the word and it's in the ordered plan below as item 9.

---

## 5. REPAIR PLAN — ordered, everything not blocked above

Each item: what changes, what it risks, what test proves it. None of this executes until the plan (including Q1–Q4) is approved.

1. **`agent_crash` last-live-agent guard (D1).** `CrashInjector._crash` checks live-agent count before crashing; raises `safety.ChaosAuthorizationError` (`E-CHAOS-001`, consistent with the existing fire-time-refusal error family) if the target is the last live agent and `allow_total_failure` is not `True`. *Risk:* none identified — purely additive guard on an existing code path; the only behaviour change is a new refusal case. *Test:* a two-agent scenario where the second crash attempt (only-agent-left) is asserted to raise, and passes when `allow_total_failure: true` is set — write it red-first.

2. **`message_drop` run_end protection (D2).** Add a message-type/event-type parameter to `decide_drop`'s call site so it can identify a `run_end`-carrying message, and refuse to drop it. *Risk:* signature change to `decide_drop` — every existing caller and unit test needs updating; check for any other production call site beyond the one in `_harness.py`/the real (currently absent) SDK hook. *Test:* regression asserting a `run_end`-tagged message is never dropped across N draws even at `probability_permille: 1000`.

3. **`latency` bound (D3).** `decide_latency`'s `degrade` pattern consults `AbortGuardMonitor`'s configured `max_virtual_duration_ms` (or a fault-local param, if the PRD intends a per-fault cap rather than the guard's — re-check §12.2's exact wording before implementing since this determines which ceiling to read) and clamps. *Risk:* changes the returned delay for any scenario already relying on unbounded `degrade` growth — check golden fixtures for a dependency. *Test:* a fixture with a long `fire_count` sequence asserting the delay plateaus rather than growing unboundedly.

4. **Fire-time authorization test teeth (D4).** Add: (a) `BlastRadius.contains` parametrised over all 5 `TargetKind` members × in/out-of-radius; (b) one integration test per fault class (`agent_crash`, `latency`/`message_drop`, `tool_failure`) that arms against an authorized target, narrows `registry.blast_radius` to exclude it, then fires and asserts `safety.ChaosAuthorizationError`. *Risk:* none — pure test addition against existing, already-correct code. *Test:* the tests themselves are the proof; acceptance criterion is that deleting any of the four `reauthorize(...)` call sites now fails at least one of them.

5. **Abort-guard trip behaviour, minus the status value pending Q1 (D5).** On `AbortGuardTripped`: disarm the injector, cancel in-flight tasks, write and seal a `run_end` event (status = `FAILED` unless Q1 resolves to (A), in which case `aborted_guard`). *Risk:* touches `Scheduler.run`'s exception handling — the highest-blast-radius file in the whole repo; needs the full 1851-test suite green, not just faults. *Test:* the existing tripped-abort-guard probe, extended to assert a `run_end` event exists, `writer.sealed is True`, and no further scheduling occurs after the trip.

6. **`payload.fault_ids` full set (D6) — only if Q-equivalent resolves toward a schema touch; recommend deferring to analysis-layer computation instead** (walk `causal_parents` at read time to reconstruct the full contributing set, rather than persisting a new per-event field into an already-shipped, append-only schema). *Risk of the deferred approach:* none to the schema; *risk of the schema approach:* another `schema_version` bump stacked on Q1's. *Test (deferred approach):* a `tests/unit/analysis` (or `tests/unit/faults`, if kept here) test with two faults contributing to one event, asserting the reconstructed set is correct — hand-computed expected output per AGENTS.md §5.

7. **`fault_summary` in run metadata (D7) — same recommendation as D6: compute at analysis/read time from `fault_injected`/`fault_effect`/`fault_not_triggered` events already in the log, rather than a new persisted field.** *Test:* hand-authored event log with 2 fired + 1 not-triggered fault, asserting `registry.summary()`'s shape matches a walk of the log.

8. **Determinism coverage for probabilistic faults (D8).** Add: (a) a golden test pinning `seeded_stream(42)`'s first 8 `next_permille()` values as hardcoded integers (no reference-implementation comparison); (b) a second `test_determinism_with_faults`-style harness arming a `PROBABILITY`-triggered `message_drop` on a fixture edge, asserting 100/100 identical canonical hashes **and** an identical drop/no-drop bit sequence across runs; (c) correct `test_probability_trigger_matches_stream_draw_exactly`'s docstring/name — it currently claims to hand-verify values and doesn't. *Risk:* none — additive tests; (a) will need a one-time genuinely-independent hand computation (e.g. via a throwaway script using a different hash-to-int derivation) to avoid the "compares itself to itself" trap. *Test:* acceptance is that reverting `% 1000` to `% 100` and reverting the `seed=seed+time.time()` mutation both now fail.

9. **D11 ledger ratification** (only if you say "yes, log it" per above) — append a `C-n` row transcribing the existing `REPEATING_TRIGGER_KINDS` ruling. *Risk:* none, additive ledger row. *Test:* n/a, ledger-only.

10. **Magic numbers / duplicated defaults (D12).** Move `DEFAULT_GUARD_EVAL_STEP_INTERVAL` and the six inline fault-param defaults into `agentdx.toml` + `scenario/loader.resolve_defaults`, following the existing `guard_default_*`/`guard_ceiling_*` convention from P08's second repair. *Risk:* any hand-authored test fixture currently relying on the old hardcoded default value needs its expected output re-derived — check each of the 6 params' existing unit tests for implicit reliance on the current default. *Test:* existing unit tests continue to pass with defaults now sourced from config; add one test asserting the config key round-trips.

11. **Remove the `pass` stub (D13).** Replace `process.py:279-288`'s `if <condition>: pass` with either a real early-return/explicit no-op comment (if the branch is genuinely a correct no-op) or real logic if it isn't — needs a read of the surrounding function to determine which. *Risk:* low, but requires understanding what `_due_fault` is meant to do for an already-fired one-shot fault before touching it. *Test:* if behaviour changes, add a regression; if it's confirmed a correct no-op, a comment explaining why plus an explicit `return None` is sufficient and no `pass` remains.

12. **Honesty corrections (D14).** Using the established C-11/C-13 pattern (append a correction, do not silently edit): either paste real G4/safety-suite output into `docs/chaos-safety.md`'s "Gates and how to reproduce them" section, or strike the three "pasted" claims in CONTEXT.md/`docs/chaos-safety.md` and replace with "reproducible via the command below" language; correct "91 tests" to the actual current count (which will change again once items 1-11 add tests — do this last and re-count). *Risk:* none, documentation-only. *Test:* n/a — visual/grep verification that the word "pasted" only appears adjacent to an actual fenced output block.

13. **Broken TOC anchors (D15).** Fix the two `docs/chaos-safety.md` internal links to match their real generated slugs. *Risk:* none. *Test:* n/a.

**Suggested execution order:** 1, 4, 8 first (highest severity, no schema dependency, no other item depends on them) → resolve Q1–Q4 → 5, 6, 7 (schema-adjacent, now unblocked) → 2, 3 (medium severity, independent) → 9, 10, 11, 12, 13 (cleanup, do last so the test-count correction in 12 is accurate).

---

## 6. PREVENTION

What would have caught this before an audit had to find it:

- **A build-time PRD-safety-row checklist.** Every fault type's PRD §12.2 entry has a labelled **Safety** row. Before a fault-execution prompt can claim `BUILT`, AGENTS.md §1's session-opening ritual should require grepping the cited PRD sections for every normatively-labelled subsection (`Safety:`, `Invariant:`, similar) and producing a one-line implementation+test citation for each — the same discipline Rule E1 already applies to published numbers, extended to normative requirement rows. This is the direct fix for tripwire #14 firing only at audit time instead of at build time.
- **A mutation-guard gate for safety-critical call sites**, run as part of `just ci` alongside `check-determinism`/`check-bench`/`check-ledger`: an AST-based script that deletes each `safety.reauthorize(...)` call site (and similarly-tagged safety-critical calls, opt-in via a `# safety-critical` comment marker) one at a time and asserts the relevant test suite goes red. This is what would have caught D4 without requiring a human auditor to think to try it. Propose `scripts/check_safety_mutations.py`, same family as the existing hygiene scripts.
- **A coverage-declaration requirement for determinism gates.** `test_determinism_with_faults.py`'s docstring claims broader coverage than its harness exercises. Require any test tagged `@pytest.mark.determinism` to state, in its docstring, exactly which fault types/trigger kinds it arms — makes the D8 gap grep-visible (a reviewer can diff the claimed list against `MVP_FAULT_TYPES`/`TriggerKind`'s full membership) instead of requiring someone to read the harness.
- **An enforced "pasted" claim.** Extend `check_bench_markers.py` (or a sibling script) to scan CONTEXT.md and `docs/*.md` for the literal word "pasted" and require a fenced code block within a few lines of it — same mechanism as Rule E1's `[bench:<filename>]` marker requirement, applied to this specific recurring failure mode (this is now the third time — C-11, C-13, and this — the same *shape* of defect, false-citation-of-evidence-location, has been caught in this project).
- Tripwire #14 itself does not need fixing — it fired correctly, at audit time. The gap is that nothing fires it *before* audit time; the three additions above close that gap going forward.
