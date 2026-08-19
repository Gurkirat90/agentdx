# OP-2 INDEPENDENT AUDIT — P09 `runtime/faults/` + chaos safety

**Auditor note on method.** Every gate below was re-run by the auditor, not read. 34 source mutations were run against the P09 suite to measure whether its tests have discriminating power, then every file was restored byte-for-byte (`md5sum -c` clean, pasted at the end). No file content was left changed; only mtimes on the 11 restored source files differ.

---

## 1. SPEC CONFORMANCE

### PRD §12.1 — Architecture / interception points

| Requirement | Status |
|---|---|
| `FaultRegistry` arms faults at interception points | **Implemented** (`registry.py:373`) |
| 7 interception points (`pre_schedule`, `pre_send`, `pre_deliver`, `pre_llm`, `pre_tool`, `pre_state_write`, `pre_resume`) | **Partially implemented, honestly declared.** Only `pre_resume` has a live analogue. `docs/chaos-safety.md:64-70` maps this correctly and the underlying grep claim (`yield_point` is called from one SDK site) is confirmed. Accept. |
| **Ordering rule:** "the `fault_injected` event is written *before* the effect is applied" | **Implemented** (`process.py:241-243`) and mutation-guarded (M19 → G4 red). |

### PRD §12.2 — Fault catalogue: the **Safety** rows

This is where conformance breaks. The Safety row is normative PRD text for each MVP fault. Three of four are unimplemented **and undeclared**:

**§12.2 `agent_crash` / Safety (PRD line 2015):**
> `Cannot crash the last live agent unless `allow_total_failure: true``

```
$ grep -rn "allow_total_failure\|last live" src/agentdx/runtime/faults/ docs/chaos-safety.md tests/unit/faults/ tests/integration/faults/
(no output; exit 1)
```

**Not implemented, not tested, not in `docs/chaos-safety.md`'s "Known gaps", not in CONTEXT.md §9.** This is aggravating rather than incidental, because CONTEXT.md §14 (line 456) explicitly handed it forward:

> *"Also still open: `agent_crash`'s 'cannot crash the last live agent' Safety-row rule is declared, not enforced — it is a P09 (fault-execution) precondition, not a `scenario/` validation gap, **and P09 must not assume `scenario/`'s validation already covers it**."*

And the data was already plumbed. Verified the parameter arrives in `FaultDecl.params` untouched:

```
$ python -c "...resolve_defaults(...); FaultRegistry.from_resolved_scenario(...)"
resolved fault entry: {'type': 'agent_crash', 'agent': 'reviewer', 'at_virtual_ts': 3000, 'recoverable': False, 'allow_total_failure': False}
FaultDecl.params    : {'recoverable': False, 'allow_total_failure': False}
```

`process.py:237 _crash()` reads `recoverable` from that same dict and ignores `allow_total_failure` two keys over. This was a one-`if` change against data already in hand.

**§12.2 `message_drop` / Safety:** *"Cannot drop a `run_end` control message"* — **not implemented.** `transport.py:197 decide_drop(*, edge, virtual_ts_ms, message_count)` has no message identity parameter at all, so the rule is not merely unchecked, it is unexpressible at that signature. Undeclared.

**§12.2 `latency` / Safety:** *"Bounded by `max_virtual_duration`"* — **not implemented.** `decide_latency` (`transport.py:137`) returns `delay_ms * (fire_count + 1)` for `degrade` with no ceiling; nothing consults `AbortGuardMonitor.max_virtual_duration_ms`. Undeclared. (The guard trips *after the fact* at the next step; the PRD says the fault is *bounded*, which is a different guarantee.)

**§12.2 `tool_failure` / Safety:** *"Only tools inside the blast radius"* — code path exists (`dependency.py:128 safety.reauthorize`) but see §4 F2/F3: it is entirely untested and deletable.

### PRD §12.3 — Trigger vocabulary

**Implemented**, transcribed match-for-match (`triggers.py:145-162`), with one **contradiction of the PRD's own pseudocode** that is declared: PRD §12.3 line 2 is `if fault.target not in ctx.blast_radius: return False   # hard invariant`, inside `should_fire`. The build moves it to arm time + `safety.reauthorize`. `triggers.py:115-119` states this explicitly. Reasonable and documented — but it converts a *return False* into a *raise*, and PRD §13.4 rule 2 does say the runtime layer raises. Accept.

`REPEATING_TRIGGER_KINDS` (`triggers.py:77`) is a genuine PRD gap (`fault.repeating` has no scenario field) resolved in a module docstring. **This is a ruling and belongs in CONTEXT.md §10 as a `C-n` row**, per AGENTS.md §3 ("If you resolve one, it goes into §10 as a new `C-n` row in the same commit"). It is not there. Same shape as C-12/C-14, which *were* logged.

### PRD §12.5 — Logging and observability

| Requirement | Status |
|---|---|
| one `fault_injected` per fault | Implemented (`_emit_fault_injected_if_first`) |
| zero-or-more `fault_effect`, one per application | Implemented |
| **"a `fault_summary` entry in run metadata"** | **Not implemented.** `registry.summary()` (`registry.py:487`) computes it; nothing consumes it. `run_end`'s payload schema has no `fault_summary` field (verified against `PAYLOAD_SCHEMAS[RUN_END]` — 8 fields, none of them this), and no store column carries it. Undeclared. |
| `fault_not_triggered` flag | Implemented in `summary()`, same non-delivery problem. |

### PRD §9.4 — Fault taint, 3 rules

Rules 1 and 2: **implemented, twice, correctly**, and genuinely mutation-guarded (M5/M6 both go red). Rule 3: implemented at tracker level, **no observable effect** — the self-disclosure is accurate and if anything understated; see §6.

**Missing and undeclared:** §9.4's closing sentence —
> *"Where multiple faults contribute, `fault_id` holds the earliest **and `payload.fault_ids` holds the full set**."*

`grep -rn "fault_ids" src/ docs/ tests/` returns exactly one hit, an unrelated test name. `compute_causal_taint` takes `min(candidates)` (`taint.py:125`) and **discards the rest**; `FaultTaintTracker.resolve` does the same (`taint.py:205`). The full set is not persisted, not computed, not declared as a gap.

### PRD §13.3 / §13.4 — Authorization, two layers

**Implemented.** Arm-time (`registry.py:420-462`) and fire-time (`safety.py:50`). Fixture-universal default (§13.3) correct at `registry.py:429`. Glob-for-state-keys-only (§13.4 rule 3) correct at `registry.py:291-306`. §13.4 rule 4 (blast radius printed by CLI / shown in UI) is P15/P17, legitimately out of scope but not listed in Known gaps.

### PRD §13.5 — Steady-state hypothesis

**Implemented** (`safety.py:83-156`), including the correct strict reading that an unmeasured declared metric is a violation. Good.

### PRD §13.6 — Abort guards (six)

All six guards are *coded*; two are *wired*. That gap is declared. But the declared gap is **narrower than the real gap** — the exact defect shape the second P08 audit already recorded twice. A live trip was probed directly:

```
$ python <abort-guard probe>
raised: [E-GUARD-001] max_virtual_duration_ms: virtual duration 1ms exceeded budget 0ms at step 2
events: [(0, 'schedule_decision', None), (1, 'schedule_decision', None)]
run state: RunState.FAILED
writer sealed? False
```

PRD §13.6 line 2181: *"the injector disarms, in-flight tasks are cancelled, **the log is sealed with `run_end.status = aborted_guard`**, and the partial log is retained and analysable."*

Actual: **no `run_end` event is written at all**, the writer is **not sealed**, the injector does not disarm, in-flight tasks are not cancelled. `docs/chaos-safety.md:159-169` declares only that `RunState` lands on `FAILED` instead of `ABORTED_GUARD`. Four further deviations are undeclared, and one of them is structural: `PAYLOAD_SCHEMAS[RUN_END]['status'].enum == {'timeout','aborted','complete','failed'}` — **there is no `aborted_guard` member**, so even a corrected scheduler could not record PRD §13.6's required status. That is a P02 schema finding P09 was positioned to make and did not.

### PRD §10.5 / §10.7 — Determinism

`check_determinism_hygiene.py` is clean for every P09 file (re-run directly; the only 3 violations are `scenario/validate.py:234-236`, pre-existing, correctly flagged-not-fixed). No banned source appears in `runtime/faults/`. `FaultRandomStream` (blake2b off the run seed) is a sound substitute for `random.*`.

One undeclared I1 caveat: `process.py:263,268` calls `wall_time()` and feeds `wall_elapsed_ms` into `AbortGuardMonitor.observe_step`, whose return value **branches run control flow** (raises `AbortGuardTripped`). That is a real-clock read influencing the canonical log inside a run context, not a volatile-field write. It is a genuine PRD §13.6 requirement, so it is not a bug — but AGENTS.md §4.1 sanctions the `wall_time()` accessor *only* for volatile-field writers, and this use is neither declared nor caveated anywhere.

### PRD §24.3 — Module map

> `agentdx.runtime.faults` | ... | **Depends on: scheduler, events**

Actual: four `runtime/faults/*.py` files import `agentdx.scenario.schema`. CONTEXT.md §4's layer table — which says of itself *"this table is the config's source of truth"* — lists `runtime/` as "May import: `events`, `store`". `.importlinter`'s `runtime-executes-only` contract is a *forbidden*-list, so it passes vacuously; that is a gap in the config, not a permission. `docs/chaos-safety.md:332-335` discloses the import and calls it *"permitted by `.importlinter` … not a new exception carved out for this prompt"* — which is true of the config file and false of the architecture map and PRD §24.3. **No §9 deviation row, no §4 table update, no ADR.**

---

## 2. INVARIANT CHECK

| # | Verdict | Mechanism (not assertion) |
|---|---|---|
| **I1 Determinism** | **Held for the exercised path; at risk on the unexercised one** | Verified: `test_determinism_with_faults.py` really does 90 in-process + 10 `subprocess.run` fresh-interpreter replays and yields one hash — `blake2b:cc0730ef…5bea38`, 1 distinct. The mechanism bites: injecting `id()` into `fault_effect_span_id` (M25) and into `exception_type` (M27) both turn it red. **But** the fault engine's only randomness source is never touched by it — rigging `FaultRandomStream.next_permille` to `raise AssertionError` left both G4 and the determinism gate green, and seeding the stream from `time.time()` (M26) passes. So I1 is *demonstrated* for `agent_crash`, *unmeasured* for every probability-driven fault. |
| **I2 Append-only log** | **Held** | All fault events go through `Scheduler.stamp` → `_SchedulerRecorder.write` → `EventWriter.write`; no second write path exists (`grep` for `_writer` in `runtime/faults/` returns nothing). The D-43 `on_event_stamped` hook fires strictly *after* `self._writer.write(event)` succeeds (`scheduler.py:463`), preserving the pure-compute-then-commit discipline. |
| **I3 Analysis purity** | **Held** | `lint-imports`: 10 kept, 0 broken. `analysis/` untouched. |
| **I7 Offline by default** | **Held** | No network surface added; nothing in `runtime/faults/` imports `httpx`/a provider. |
| **I9 Evidence / Rule E1** | **At risk** | `check_bench_markers.py` passes over `docs/chaos-safety.md` (scanner covers `docs/**.md`). But CONTEXT.md is not scanned, and it carries two unsourced numbers: "20/20 … pasted in `docs/chaos-safety.md`" (nothing is pasted) and "91 tests" (actual 79). See §6. |
| **I11 Virtual/wall separation** | **Held, with the caveat above** | `fault_effect.delay_virtual_ms`, `first_fired_virtual_ts_ms`, `ready_at_virtual_ms`, `max_wall_duration_s` all correctly suffixed. The `wall_time()` control-flow read is the one undeclared crack. |
| **I12 Chaos is fixture-only, blast radius enforced** | **Held in code, VIOLATED as a guarantee** | The invariant table names its enforcement mechanism as *"Scenario validation (`E-SCEN-004`) + **runtime re-check (`E-CHAOS-001`)**"*. Deleting the runtime re-check from all three execution modules left the entire suite green (M3, §4). Making TOOL, EDGE and PROVIDER membership unconditionally `True` left the suite green (M31/M32/M33). Making a fixture's *explicitly declared* narrow blast radius silently widen to universal left the suite green (M28). The code is correct today; nothing prevents it from silently ceasing to be. |
| **I13 No model in the path** | **Held** | N/A + `lint-imports` contract kept. |

---

## 3. SCOPE VIOLATIONS

**No unlisted file was touched.** Every file the P09 session wrote was enumerated (it ran as `root`; every pre-P09 file is owned by uid 1004):

```
$ find . -user root -newermt "2026-08-16 15:00" -type f \( -name "*.py" -o -name "*.md" ... \)
./CONTEXT.md
./docs/chaos-safety.md
./docs/journal/2026-33.md
./src/agentdx/runtime/faults/{dependency,process,registry,safety,semantic,state,taint,transport,triggers}.py
./src/agentdx/runtime/scheduler.py
./tests/{unit,integration}/faults/*
```

Exactly the deliverable list plus the two ledger files. `agentdx.toml`, `config.py`, `events/*`, `scenario/*` all untouched (mtimes ≤ 2026-08-16 14:32, uid 1004). Clean.

**Is the `scheduler.py` touch as narrow as D-43/D-44 claim? Yes.** Two additive default-`None`/no-op methods on `FaultInjectorHook` (`scheduler.py:264`, `:296`) and two call sites inside the single existing `_SchedulerRecorder.write` (`:440`, `:463`), plus the one-line `declared_causal` computation (`:439`). Nothing else in the file references faults. The claim that every pre-P09 caller is unaffected is verified: the full 1851-test suite passes.

**But the touch's *architectural* footprint is wider than D-43/D-44 admit** — see §1 PRD §24.3: `runtime → scenario` is a new layer edge with no deviation row. That is the scope violation, and it's in the ledger rather than the file list.

**Unrequested features:** none. `semantic.py`/`state.py` are genuinely docstring-only (`__all__: list[str] = []`, no executable logic) — a correct reading of AGENTS.md §2's stub ban, not a violation of it.

**Refactor of earlier code disguised:** none found.

---

## 4. THE TEST-QUALITY QUESTION

The unit suite is better than average for this codebase — 26 of 34 mutations went red, including every "earliest fault wins" tie-break, both latency patterns, the `count` budget, the MVP tier gate, and the D-44 regression. **Eight survived**, and they cluster in two places that matter.

### The two most important tests, and the bug each misses

---

#### **Test A — `tests/integration/faults/test_safety_suite.py` (the whole file) + `tests/unit/faults/test_safety.py::test_reauthorize_raises_e_chaos_001_for_a_target_outside_the_blast_radius`**

This is the suite that carries invariant I12 and PRD §13.4's "enforced at two layers".

**The mutation it misses.** Delete the fire-time re-check from every execution module — the entire second layer:

```python
# process.py:239, transport.py:175 & 228, dependency.py:128 — all four lines removed
-safety.reauthorize(armed, self._registry.blast_radius)
```

```
$ pytest tests/unit/faults tests/integration/faults -q
94 passed
```

Green. `safety.py:6-12` claims the opposite in prose:

> *"`reauthorize` is not a wrapper an execution module might forget to call — it is the only path … If a future fault-class module skipped it, that is a code-review-visible omission … not a silently-widened blast radius."*

Code-review-visible is exactly what it is, and nothing more. The safety suite tests `reauthorize` by *calling it directly* (`test_safety_suite.py:96`) and tests arm-time refusal by *never constructing an injector*. No test ever puts a real injector in front of an out-of-radius target.

**Related survivors, same root cause** — `BlastRadius.contains` is only ever tested for `AGENT` and `STATE_KEY` (`grep "contains(" tests/` returns 6 lines, all AGENT/STATE_KEY/universal):

| Mutation | Result |
|---|---|
| `if kind is TargetKind.TOOL: return True` | **94 passed** — and PRD §12.2's `tool_failure` Safety row is literally *"Only tools inside the blast radius"* |
| `if kind is TargetKind.EDGE: return True` | **94 passed** — `message_drop` targets edges |
| `if kind is TargetKind.PROVIDER: return True` | **94 passed** |
| `universal = is_fixture_target` (drop `and not blast_radius_declared`) | **94 passed** — a fixture scenario's *explicit* narrow blast radius silently widens to "everything" |

Every transport/dependency unit test builds its registry with `is_fixture_target=True` and no blast radius (`test_transport.py:15`, `test_dependency.py`), so `universal=True` makes every authorization check trivially pass.

**What would catch it:** one integration test per fault class that arms a fault against an authorized target, then mutates `registry.blast_radius` to a `BlastRadius` excluding it before invoking `decide_tool_call`/`decide_drop`/`pre_yield`, and asserts `safety.ChaosAuthorizationError`. Plus a `BlastRadius.contains` parametrisation over all five `TargetKind` members × (in-radius, out-of-radius) — 10 assertions, and all four mutations above die.

---

#### **Test B — `tests/integration/faults/test_determinism_with_faults.py::test_100_runs_with_faults_enabled_are_byte_identical_10_of_them_in_fresh_processes`**

Its own docstring is the claim under audit:

> *"If arming `FaultRandomStream`, `FaultTaintTracker`, or any fault-class module's own state introduced a single non-deterministic read (wall-clock, unseeded random, dict/set iteration order), it would show up here as a hash mismatch."*

**That is false, and here is the proof.** First, the stream is never drawn at all in this gate — making a draw fatal:

```python
# triggers.py:61 FaultRandomStream.next_permille
-        self._counter += 1
+        raise AssertionError('STREAM WAS DRAWN')  # probe
```
```
$ pytest tests/integration/faults/test_determinism_with_faults.py tests/integration/faults/test_gate_g4.py -q
100%
```

Both gates pass with the RNG rigged to explode. Consequently:

```python
# triggers.py:74 seeded_stream — a textbook I1 violation, exactly what AGENTS.md §4.1 bans
-    return FaultRandomStream(seed=seed)
+    import time; return FaultRandomStream(seed=seed + int(time.time()))
```
```
$ pytest tests/integration/faults/test_determinism_with_faults.py -q
100%
```

**Green.** The harness (`_harness.py:146-155`) arms exactly one `agent_crash` with an `AT_VIRTUAL_TS` trigger — a fault class with no probability path anywhere in it. The "determinism *with faults*" gate exercises the one MVP fault that has no randomness.

**The plausible bug it also misses**, in the same blind spot — an off-by-a-factor in the permille modulus:

```python
# triggers.py:65
-        return int.from_bytes(digest, "big") % 1000
+        return int.from_bytes(digest, "big") % 100
```
```
$ pytest tests/unit/faults tests/integration/faults -q
94 passed
```

Every draw now lands in `[0,100)`. A scenario declaring `message_drop(probability_permille: 300)` — a 30% drop rate — drops **100%** of messages. A `tool_failure(probability: 500)` fires on every call. The chaos experiment's entire premise is inverted and the run still reports "fault fired as declared".

Why it survives: the test that claims to guard this, `test_triggers.py:84 test_probability_trigger_matches_stream_draw_exactly`, opens with the comment `# Hand-computed:` and then does **not** hand-compute anything —

```python
live_stream = seeded_stream(7)
reference_stream = seeded_stream(7)
for _ in range(10):
    expected = reference_stream.next_permille() < 500  # the implementation
    actual = should_fire(armed, virtual_ts_ms=0, stream=live_stream)  # the implementation
    assert actual == expected
```

Both sides call the same production function, so both sides move together under any change to it. It proves one real thing (exactly one draw is consumed per `should_fire` call) and nothing about the values. `test_fault_random_stream_values_are_in_range` asserts `0 <= v < 1000`, which `% 100` satisfies. This is the identical defect the second P08 audit already caught and named — *"the new Safety-bound regression tests used only extreme sentinels, never the real PRD ceiling"* — recurring here for `probability_permille` (only 0 and 1000 are ever tested, `test_transport.py:108,125`).

**What would catch both:** (a) a golden test pinning the literal first 8 values of `seeded_stream(42)` — hardcoded integers, no reference implementation; (b) a second determinism harness whose scenario arms a `PROBABILITY`-triggered `message_drop` on a fixture edge and asserts 100/100 identical canonical hashes *and* an identical drop/no-drop bit-sequence.

### Other tests that assert the code does what the code does

- **`_harness.py:217`** — `took_fallback = len(tester_events) > 0  # this harness's tester always logs one TOOL_CALL`. The comment is an admission: `CascadeShape.tester_took_fallback_path` is `True` in every possible run, crash or no crash. `test_gate_g4.py:40` then asserts `is True` under the banner *"Not a vacuous pass"*. One third of gate G4's "cascade shape" is a constant. There is no cascade in this harness — `_tester` sleeps on a timer and never waits for a review message (`_harness.py:126-142`), and its `marker.append("fallback")` is written into a list that `run_scenario_async` discards.
- **`test_gate_g4.py:56 test_a_different_seed_can_produce_a_different_cascade_shape`** — never uses a different seed. The body runs `SEED` once and asserts `baseline.total_event_count > 4`. The docstring quietly concedes this. A test whose name is a claim its body does not make.
- **`test_triggers.py:104 test_repeating_trigger_kinds_is_exactly_probability_and_always`** — `assert REPEATING_TRIGGER_KINDS == frozenset({PROBABILITY, ALWAYS})`, i.e. the constant equals its own literal. Zero information.
- **`process.py:355 fault_id_for` / rule 3** — deleting `self._taint.mark_agent_tainted(...)` from `CrashInjector._crash`, the **only production call to rule 3 in the entire codebase**, leaves 94/94 green (M20). This confirms the session's self-disclosure and sharpens it: rule 3 is not merely unobservable, its wiring is provably dead code.

### What the tests do prove

Credit where due: the D-44 regression genuinely bites. Reverting `declared_causal = sorted(set(causes)) if causes else []` to `causal` turns **four** tests red across three files, with a legible diff (`{'schedule_decision': 1, 'tool_call': 1}` extra taint). That is a properly-constructed regression test, and the self-reported fix holds up.

---

## 5. DRIFT TRIPWIRES (CONTEXT.md §11, every item)

| # | Fired? | Evidence |
|---|---|---|
| 1 | **Weakly** | No test was weakened. But `test_a_different_seed_can_produce_a_different_cascade_shape` has an assertion that does not match its name, and `test_probability_trigger_matches_stream_draw_exactly`'s `# Hand-computed:` comment describes an assertion it doesn't make. Same family. |
| 2 | **Weakly** | No banned source under `runtime/faults/`; `check_determinism_hygiene.py` clean for all P09 files. But `wall_time()` at `process.py:263,268` feeds run-aborting control flow — a real-clock read inside a run context, sanctioned by AGENTS.md §4.1 only for volatile-field writers. Undeclared. |
| 3 | No | `lint-imports`: 10 kept, 0 broken. |
| 4 | No | N/A — no analysis layer yet. |
| 5 | **Fires** | `safety.py:167 DEFAULT_GUARD_EVAL_STEP_INTERVAL = 100` inline, not in `agentdx.toml` — the *exact* shape the second P08 audit flagged for `_SHELL_TIMEOUT_S = 5` and forced into config. Plus six inline fault-param defaults (`registry.param_int(..., "restart_after_ms", **0**)`, `"pattern", **"constant"**`, `"probability_permille", **1000**`, `"count", **1**`, `"mode", **"timeout"**`, `params.get("recoverable", **True**)` in three places) — and `scenario/loader.py:547-549 resolve_defaults` is the established home for exactly these, already setting `recoverable` itself. Duplicated source of truth, D-12/D-39/D-41's named anti-pattern. |
| 6 | No | `events/schema.py` mtime 2026-08-10, uid 1004 — untouched. |
| 7 | No (for P09) | `check_bench_markers.py` passes over `docs/chaos-safety.md`. The 2 live failures are pre-existing (`docs/journal/2026-33.md:42`, `docs/scenario-reference.md:244`). |
| 8 | No | N/A. |
| 9 | No | MVP set held at 4; `MVP_FAULT_TYPES` is enforced structurally (M18 → red). |
| 9b | No | N/A. |
| 10 | No | No cut taken. |
| 11 | No, on files | Exact deliverable list, verified by ownership+mtime scan (§3). |
| 12 | No | N/A. |
| 13 | **Cannot be checked, and that matters** | `check_ledger.py` → `FAILED — cannot resolve 'origin/main'`. §8/§9 *appear* additive (D-43, D-44 appended, nothing renumbered), but this is an eyeball, not the gate. Pre-existing sandbox gap. |
| 14 | **FIRES HARD — the headline** | *"A PRD requirement inside a completed prompt's scope was neither implemented nor declared in §9."* At least nine: `allow_total_failure` / last-live-agent (§12.2); `payload.fault_ids` (§9.4); `message_drop` run_end protection (§12.2); `latency` bounded-by-max_virtual_duration (§12.2); `fault_summary` in run metadata (§12.5); `run_end.status = aborted_guard` + writer sealing + injector disarm + in-flight cancel (§13.6); `E-CHAOS-002`/`E-CHAOS-003` invented without a §9 row (precedent D-40); `runtime → scenario` vs PRD §24.3; the `fault.repeating` ruling without a §10 `C-n` row. |
| 15 | **Fires (pre-existing)** | `just ci` is locally red on three gates: `check-determinism` (exit 2, 3 pre-existing `scenario/validate.py` violations), `check-bench` (exit 2, 2 pre-existing), `check-ledger` (no git). None caused by P09; P09 correctly flagged the first. No CI to read. |
| 16 | No | `grep -rEn 'class \w+\((dict\|list\|set)\b' src/agentdx/` → none. |

---

## 6. HONESTY AUDIT

**H1 — "pasted" is false, three times.** CONTEXT.md §5 row 9:
> *"`pytest tests/integration/faults/test_gate_g4.py -q -s` passes locally (20/20 identical cascade shape, **pasted in `docs/chaos-safety.md`**)"*

§7 P09 paragraph: *"a safety-suite integration test (… **all four pasted**)"*. `docs/chaos-safety.md:104-105` itself: *"`test_safety_suite.py` exercises both layers end to end, with **actual raised-error text pasted in** [Gates and how to reproduce them]."*

```
$ grep -rn "CascadeShape(crashed_agents" --include=*.md .
(no output)
```

`docs/chaos-safety.md`'s "Gates and how to reproduce them" section (lines 290-314) contains **only commands, no output**. Nothing is pasted in any committed file — not the doc, not the journal, not CONTEXT.md. The commands do pass (re-run directly; G4 prints `20/20 runs at seed=42 … tainted_event_type_counts=(('fault_effect', 1), ('fault_injected', 1))`), so this is not a fabricated result — it is a **false citation of where the evidence lives**, which is precisely the defect class this project has already had to correct twice (the C-11 amendment and the C-13 correction, both in §10).

**H2 — "91 tests" is wrong by 12.** CONTEXT.md §7 and §13 both say *"`tests/unit/faults/` (91 tests)"*.
```
$ pytest tests/unit/faults
79 passed in 0.18s
```
Per file: dependency 8, process 7, registry 14, safety 17, taint 11, transport 9, triggers 13 = 79. (94 including the 15 integration tests; still not 91.)

**H3 — a test docstring overclaims a determinism guarantee it cannot give.** `test_determinism_with_faults.py:9-12` — see §4 Test B. Proven false by two mutations.

**H4 — `safety.py`'s module docstring overclaims structural enforcement.** *"`reauthorize` is not a wrapper an execution module might forget to call"* — it is exactly that, proven by M3.

**H5 — the "Not a vacuous pass" comment at `test_gate_g4.py:38` is attached to a vacuous assertion.** `tester_took_fallback_path` cannot be `False`.

**H6 — the declared abort-guard gap understates the real one** by four items (§1 §13.6).

**H7 — `docs/chaos-safety.md:332-335`** frames the `runtime→scenario` import as *"permitted by `.importlinter` … not a new exception carved out for this prompt"*, which is true of the linter file and silent about PRD §24.3 and CONTEXT.md §4 both saying otherwise.

**Re-run of every pasted claim that could be checked:**

| Claim | Verdict |
|---|---|
| `ruff check` / `ruff format --check` clean | **TRUE** — `All checks passed!` / `210 files already formatted` |
| `mypy --strict` (58 source files) | **TRUE** — `Success: no issues found in 58 source files` |
| `lint-imports` 10/10 | **TRUE** — `Contracts: 10 kept, 0 broken.` |
| `check_determinism_hygiene.py` clean for P09 files | **TRUE** — only the 3 declared `scenario/validate.py` lines |
| G4 20/20 identical cascade shape | **TRUE** — reproduced, 1 distinct shape |
| determinism-with-faults 100/100, 10 fresh subprocesses | **TRUE** — `100/100 … produced 1 distinct canonical hash(es): {'blake2b:cc0730ef…'}`; the subprocess path is real (`subprocess.run([sys.executable, '-m', ...])`, `PYTHONHASHSEED=0` in the child env) |
| Full repo suite green | **TRUE** — `1851 passed in 52.10s` |
| No stub/TODO/mocked return shipped as done | **TRUE with one exception** — `process.py:279-288` is `if <condition>: <comment> pass`, a literal `pass` under `src/agentdx/`, which AGENTS.md §2 bans by name. Ruff's `PIE` ruleset is not selected so nothing caught it. Semantically harmless today (`_due_fault` returns `None` for an already-fired one-shot fault) but it is placeholder code shipped as done. |
| `docs/chaos-safety.md` covers every `_DOCS` anchor | **TRUE** — all 6 code-referenced anchors resolve. Two *TOC* links are broken though: `#the-authorization-model-two-layers` (real slug `--two-layers`, em-dash) and `#the-scheduler-py-deviation` (real slug `the-schedulerpy-deviation`), each referenced 3x from the body. |

---

## 7. HANDOFF READINESS

A fresh agent with only CONTEXT.md and the PRD would get these wrong:

**The named, load-bearing undocumented assumption: it would believe I12's blast-radius enforcement is test-guarded, and would build on top of it.** CONTEXT.md §2 lists I12's mechanism as *"Scenario validation (`E-SCEN-004`) + runtime re-check (`E-CHAOS-001`)"*, and `safety.py`'s docstring tells the next author that skipping `reauthorize` is structurally visible. Neither is true. The next fault-class module (P1 types, or the `sdk/generic.py` interception work) will be written by someone who reasonably assumes the suite will tell them if they forget the call. It will not. That is how a blast radius silently widens.

Four more:

1. **It would assume `allow_total_failure` is enforced somewhere.** `scenario/loader.py` resolves it, `scenario/schema.py` type-checks it, `FaultDecl.params` carries it, `docs/scenario-reference.md:129` documents it as an accepted param. It is read by nothing. The only place saying so is CONTEXT.md §14's P08 bullet 5, 456 lines in.
2. **It would assume gate G4 demonstrates a cascade.** It does not. `_harness.py`'s `tester` never depends on `reviewer`; the "cascade" is a hardcoded `True` and a discarded marker list. The measured taint footprint of the entire G4 run is 2 events — the fault's own. A P11 resilience-scoring prompt taking G4's output as a cascade example will be modelling nothing.
3. **It would assume the determinism-with-faults gate covers probabilistic faults.** It covers one `AT_VIRTUAL_TS` `agent_crash`. Anyone adding a probability-triggered fault has *no* determinism coverage and will believe they inherited it.
4. **It would not know `run_end.status` cannot express `aborted_guard`.** Whoever fixes the `RunState.ABORTED_GUARD` gap (the declared one) will find a second, harder schema blocker mid-repair — a `schema_version` bump and tripwire 6.

---

## VERDICT

# FAIL — do not mark VERIFIED; do not proceed to P10 without an OP-3.

This is a genuinely strong build in most respects — the taint engine is right, the D-44 self-fix holds up under mutation, the declared-gap discipline is unusually honest, scope is clean, and 26 of 34 mutations died. It fails on two things: a normative PRD safety rule that this project's own ledger explicitly handed to this prompt and told it not to skip, and a test suite whose two most load-bearing claims (I12 enforcement, determinism-under-faults) do not hold when probed.

### Minimum fixes, in priority order

1. **Implement PRD §12.2's `agent_crash` Safety row.** `CrashInjector` must refuse to crash the last live agent unless `armed.decl.params["allow_total_failure"]` is `True`. The parameter is already in `FaultDecl.params`. Add a regression test that fails before the fix. (§1, tripwire 14, CONTEXT.md §14's explicit instruction.)
2. **Give the fire-time authorization layer test teeth.** One integration test per fault class asserting `safety.ChaosAuthorizationError` when an armed fault's target leaves the radius before firing, plus a `BlastRadius.contains` parametrisation over all five `TargetKind`s. Acceptance: mutations M3, M28, M31, M32, M33 must all go red. (I12.)
3. **Give the RNG stream real coverage.** Pin `seeded_stream(42)`'s first 8 draws as hardcoded integers, and add a determinism harness arming a `PROBABILITY`-triggered `message_drop`. Acceptance: M26 (`seed + time.time()`) and M34 (`% 100`) must both go red. Then correct `test_determinism_with_faults.py`'s docstring to state what it actually covers.
4. **Correct the three false "pasted" claims and the "91 tests" count** in CONTEXT.md §5/§7/§13 and `docs/chaos-safety.md:104`. Either paste the real output into `docs/chaos-safety.md` or delete the claim. Same mechanism as the C-11 amendment — appended correction, not a silent edit.
5. **Log the missing §9 deviation rows and §10 ruling** — one per: `allow_total_failure`; `payload.fault_ids`; `message_drop` run_end protection; `latency` duration bound; `fault_summary` in run metadata; `run_end.status` has no `aborted_guard` member + writer-not-sealed + injector-not-disarmed on trip; `E-CHAOS-002`/`E-CHAOS-003` (precedent D-40); `runtime → scenario` vs PRD §24.3 and CONTEXT.md §4; and a `C-n` row for the `fault.repeating` ruling currently living only in `triggers.py`'s docstring.
6. **Fix the two vacuous G4 assertions.** Either make `_tester` genuinely depend on a review message so `tester_took_fallback_path` can be `False`, or delete the field and stop calling it "not a vacuous pass". Make `test_a_different_seed_can_produce_a_different_cascade_shape` use a different seed or rename it.
7. **Cheap cleanups:** delete the `if …: pass` block at `process.py:279-288`; move `DEFAULT_GUARD_EVAL_STEP_INTERVAL` and the six fault-param defaults to `agentdx.toml` / `scenario.loader.resolve_defaults` (tripwire 5); fix the two broken TOC anchors in `docs/chaos-safety.md`; document that the three injectors each construct an independent `seeded_stream(seed)` from the same seed, so their probability draws are perfectly correlated.

**Not blocking, but the next auditor should start here:** the `wall_time()` read that branches run control flow (`process.py:263-268`), and `compute_causal_taint`'s `injected_at.get(fid, event.seq)` default vs `FaultTaintTracker.resolve`'s `1 << 62` — the two "earliest wins" implementations disagree on the unknown-fault tie-break, which is exactly the kind of divergence a two-implementations-of-one-rule design is supposed to be tested against and isn't.

---

**Read-only compliance.** All 34 mutations were reverted from a pre-audit copy. Final state:

```
$ md5sum -c /tmp/op2_backup/checksums.txt
src/agentdx/runtime/faults/{__init__,dependency,process,registry,safety,semantic,state,taint,transport,triggers}.py: OK
src/agentdx/runtime/scheduler.py: OK
$ ruff check .                 → All checks passed!
$ mypy --strict src/agentdx    → Success: no issues found in 58 source files
$ pytest tests/                → 1851 passed in 52.10s
```
