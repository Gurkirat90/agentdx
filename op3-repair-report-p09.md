# P09 OP-3 repair — completion report

Repair of the independent OP-2 audit findings against `runtime/faults/` (`op2-audit-p09.md`), executed against the approved `op3-plan-p09.md` and your four Q1–Q4 answers. This report follows `AGENTS.md` §7's four-block contract.

---

## SELF-AUDIT

**Done, mutation-verified where the finding was test-quality-shaped** (a deliberate mutation of the fix confirmed the new test goes red, then the fix was restored and re-verified green):

- **`agent_crash` Safety row** — "cannot crash the last live agent unless `allow_total_failure: true`" (PRD §12.2). `CrashInjector._due_fault`/`_would_leave_no_live_agent` now enforce it as a silent skip-and-retry (not an exception — an ordinary runtime condition, not a blast-radius "should be unreachable" check). Caught and fixed a self-introduced ordering bug in the same pass (the guard's answer depended on scheduling order until `_task_agent` recording was split into its own pass).
- **`message_drop` Safety row** — "cannot drop a `run_end` control message". `decide_drop(..., carries_run_end=True)` refuses unconditionally before any trigger/probability evaluation.
- **`latency` Safety row** — "bounded by `max_virtual_duration`". `TransportFaultInjector(max_virtual_duration_ms=...)` clamps the applied delay so `virtual_ts_ms + extra_delay_ms` never exceeds the budget.
- **Fire-time authorization test coverage** (the audit's headline test-quality finding: all four `safety.reauthorize()` call sites were deletable with the full suite green). New tests for `CrashInjector`, `TransportFaultInjector` (both `decide_latency` and `decide_drop`), and `DependencyFaultInjector`: arm inside the blast radius, narrow it after arming, fire, assert `safety.ChaosAuthorizationError`. Plus a `BlastRadius.contains` parametrization covering `TOOL`/`EDGE`/`PROVIDER` (previously only `AGENT`/`STATE_KEY` were exercised).
- **Abort-guard → `RunState.ABORTED_GUARD`** (the non-schema half of the abort-guard finding). `Scheduler.run()` gained a specific `except AbortGuardTripped` branch, before the generic `except BaseException`, transitioning to the already-legal-but-never-reached `RunState.ABORTED_GUARD` instead of `FAILED`, and closing every unfinished task's coroutine first.
- **`payload.fault_ids`** (PRD §9.4's "the full set", vs. `fault_id`'s "the earliest") — implemented as an offline pure function, `taint.compute_full_taint`, rather than a second schema field (**D-46**).
- **Tie-break defaults** — the two independent "earliest fault wins" implementations (`compute_causal_taint` and `FaultTaintTracker.resolve`) now share one `_UNKNOWN_INJECTION_SENTINEL` constant.
- **RNG determinism gap** — `test_determinism_with_faults.py` only ever armed `agent_crash`, which never draws from `FaultRandomStream`, so a stream regression (wrong modulus, byte order, counter) could not have been caught by it. Closed with a hardcoded-reference test (`seeded_stream(42)`'s first 8 values, computed independently, not self-referentially) plus a new `test_message_drop_determinism.py` proving 100/100 identical canonical hashes *and* identical drop-bit-sequences for a `probability_permille`-drawing fault. `test_determinism_with_faults.py`'s own docstring corrected to state its actual, narrower coverage.
- **Q2** — ratified the `runtime → scenario` import as deliberate (`ADR-013`), with an explanatory comment in `.importlinter` and a `CONTEXT.md` §4 layer-table update.
- **Q3** — added a 5th sanctioned `AGENTS.md` §4.1 determinism exemption for the abort-guard's `wall_time()` read (`ADR-014`), with the accepted I1 tension stated explicitly (the run's abort/no-abort outcome right at the `max_wall_duration_s` boundary can differ by machine speed — the one guard where that's true).
- **Q4** — `fault.repeating`'s ruling transcribed into `CONTEXT.md` as **C-16**; the two tie-break defaults aligned (see above).
- Found and fixed 12 pre-existing `ruff I001` import-order violations across the P09 test suite (unrelated to the OP-3 plan, caught while re-verifying — I'd earlier told you these were clean when they weren't; that was wrong and is now actually fixed).

**Blocked, not done — re-raising this to you now:** **Q1** (add `aborted_guard` to `run_end.status`'s enum) was approved, but implementing it — bumping `SCHEMA_VERSION` 1→2 and adding the enum member — broke 6 pre-existing tests (3 golden-fixture replays, a golden-log validation test, a migration-harness test, a bundle-safety test). Root cause: `check_structural`'s `E-EVENT-008` rejects any event whose `schema_version` isn't exactly the build's current version, and there's no migrate-on-read path — the golden fixtures hand-construct raw `Event` objects at the old schema version and validate them directly. `AGENTS.md` §5 forbids regenerating golden files without an explicit written instruction, and that instruction wasn't part of your Q1 answer (which was scoped to "accepting a schema_version bump," not "regenerate the golden corpus"). I reverted the schema/migration edit cleanly rather than either push through past that gate or silently fall back to the unchosen option. This is recorded as **D-45** with three options for you to choose among — see NOT DONE below.

## VERIFY THIS YOURSELF

```
cd agentdx && source .venv/bin/activate
PYTHONHASHSEED=0 python -m pytest -q          # 1872 passed
ruff check src tests                           # All checks passed!
ruff format --check src tests                  # 161 files already formatted
mypy --strict                                  # Success: no issues found in 58 source files
lint-imports                                   # Contracts: 10 kept, 0 broken
```

Mutation checks I ran and reverted (not left in the tree — you can re-run any of them by temporarily deleting the named line and re-running the matching test):
- Deleting each of the 4 `safety.reauthorize()` call sites individually → each matching new fire-time-authorization test goes red, confirmed one at a time, then restored.
- Changing `FaultRandomStream.next_permille`'s `% 1000` to `% 100` → `test_fault_random_stream_matches_hardcoded_reference_values_for_seed_42` goes red.
- Deleting `Scheduler.run()`'s new `except AbortGuardTripped` branch → both new `test_safety_suite.py` assertions (`RunState.ABORTED_GUARD`, every task closed) go red.

`scripts/check_determinism_hygiene.py` still reports the same 3 pre-existing `scenario/validate.py` violations from before this session — unrelated to `runtime/faults/`, left untouched, flagged not fixed (out of P09's scope).

`scripts/check_ledger.py` still cannot run (`cannot resolve 'origin/main'` — no git remote in this sandbox), a pre-existing condition unrelated to this session's edits.

## CONTEXT LEDGER PATCH

Applied to `CONTEXT.md` directly (this file is the patch — no separate diff needed):
- §4 layer table: `runtime/` now lists `scenario` as a permitted import (ADR-013).
- §5 row 9 (P09): status updated to reflect the OP-2 FAIL + OP-3 repair, D-45 flagged as still blocking full closure.
- §7 Current Position: rewritten; **Blocked on** now names D-45 explicitly.
- §8: **ADR-013** (ratify `runtime→scenario`), **ADR-014** (5th determinism exemption).
- §9: **D-45** (Q1 schema-change attempt-and-revert, blocked), **D-46** (`compute_full_taint`), **D-47** (`fault_summary` computed but not persisted — no run-sealing call site exists yet), **D-48** (retroactive `E-CHAOS-002`/`E-CHAOS-003` declaration).
- §10: **C-16** (`fault.repeating` ruling).
- §13: new session-log row (2026-08-17, P09 + OP-2 + OP-3); the P03 row rolled into `docs/journal/2026-33.md` to keep the table at 15 rows, per the established convention.
- `AGENTS.md` §4.1: 5th sanctioned determinism exemption added, in full.
- `.importlinter`: explanatory comment on `runtime-executes-only` naming ADR-013.

## NOT DONE / RISKS

1. **D-45 is still open and needs your decision.** Three options, as recorded in `CONTEXT.md`:
   - **(a)** Issue the explicit written instruction to regenerate the affected golden fixtures under `SCHEMA_VERSION = 2` (satisfies `AGENTS.md` §5's "explicit written instruction" requirement) — I'd then re-apply the schema bump and the `aborted_guard` enum member, and regenerate the 3 golden fixtures + related test expectations.
   - **(b)** Write a real `migrate_v1_to_v2` migration and thread migrate-on-read into `validate_log`/the golden-fixture test harness, so old-version fixtures keep validating without regeneration — more work, but the golden corpus never needs a written-instruction override.
   - **(c)** Decline the schema change; rely on `RunState.ABORTED_GUARD` alone (already reachable via this session's scheduler fix) as the abort-guard signal, accepting that `run_end.status` and `RunState` can disagree in that one case.
   - I'd recommend (b) if you want this closed cleanly without touching golden files, or (a) if you're fine issuing that instruction — either avoids (c)'s split-brain between the two state representations. Your call.
2. **A re-audit of this repair is owed** before `runtime/faults/` may be marked `VERIFIED` — same standing pattern every module in this project has followed since P02. I did not audit my own repair; that would not count.
3. **`fault_summary` (PRD §12.5) is computed but not persisted** (D-47) — `FaultRegistry.summary()` works and is tested, but nothing in this build seals a run and writes it anywhere yet. Not a P09 gap specifically; no `RunHost`/P17 CLI exists to call it from.
4. **`docs/chaos-safety.md`'s known-stale claims** (3 false "pasted" claims, a wrong "91 tests" count, 2 broken TOC anchors — all flagged by the original OP-2 audit) were **not corrected** in this pass; deprioritized in favor of the code/test/ledger work given session length. Flagging so it doesn't silently look done.
5. Everything above is synced to this sandbox's working tree and verified; I could not sync it to your device this turn because the desktop bridge reports "not connected" — reopen the Claude desktop app when you're ready and I'll push the changed files over, or I can send them as a tarball now if you'd rather pull them manually.
