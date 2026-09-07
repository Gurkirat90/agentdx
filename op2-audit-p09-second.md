# OP-2 INDEPENDENT AUDIT (SECOND PASS) — P09 `runtime/faults/` + chaos safety

**Date:** 2026-09-05
**Module / prompt:** `runtime/faults/` (4 MVP fault-injection engine + I12 chaos safety), prompt P09
**Auditor:** independent OP-2 pass, fresh read, no memory of the original build or repair
**Prior history:** first independent OP-2 (2026-08-17, `op2-audit-p09.md`) returned **FAIL** on
34 mutation-tested findings. Repaired same day per `op3-repair-report-p09.md` (all but one
finding closed) plus `op3-repair-report-p09-addendum-d45.md` (the schema/migration piece,
closing D-45 via D-49). CONTEXT.md §5 row 9 records the module as "repaired... not `VERIFIED`,"
with a re-audit "owed... same standing pattern as every module since P02." **Never re-audited
until now.**

**Scope.** `src/agentdx/runtime/faults/{registry,safety,process,transport,dependency,triggers,
taint,semantic,state}.py`, `src/agentdx/runtime/scheduler.py`'s fault-hook surface,
`tests/unit/faults/*.py`, `tests/integration/faults/*.py`, `docs/chaos-safety.md`, and — because
D-49's fix lives there — `src/agentdx/events/{schema.py,canonical.py}` and
`src/agentdx/events/migrations/__init__.py`.

---

## Method — what was actually executed, not just read

Read first, in full: `CONTEXT.md` (496 lines, all of §0, §2, §4, §5 row 9, §6 (G4), §8 ADR-013/
ADR-014, §9 D-45/D-46/D-47/D-48/D-49/D-66, §11 tripwires, §13's P09 session-log entry),
`AGENTS.md` (130 lines, all of it), PRD §12 (fault catalogue, lines ~1990–2030) and §13 (chaos
safety, lines ~2150–2200) at the exact cited passages, `docs/chaos-safety.md` (all 341 lines),
and both prior artifacts (`op2-audit-p09.md`, 386 lines; `op3-repair-report-p09.md`, 69 lines;
`op3-repair-report-p09-addendum-d45.md`, 44 lines; `op3-plan-p09.md`).

This sandbox is Python 3.10.12; the project is pinned `>=3.12,<3.13` (D-66, standing). The
disclosed shim (`sitecustomize.py` backporting `tomllib`/`datetime.UTC`/`enum.StrEnum`,
`PYTHONPATH=/tmp/shim:src`) let the **real, unmodified test suite run for real** — not a
proxy. `ruff`, `mypy --strict`, and `lint-imports` were all installed in this sandbox and run
directly against the real source, not simulated.

**Every finding below is demonstrated live**, against the real, imported production code —
either a direct-repro script (constructing real `CrashInjector`/`TransportFaultInjector`/
`Scheduler` objects and running them) or a source mutation (`cp` a backup, `sed`/Python-edit
the line, run the real suite, restore from backup, `git status`/`git diff --stat` confirmed
clean afterward — never committed, never left in the tree). No repository file was edited at
any point that was not restored before this report was written.

---

## VERDICT: **FAIL — and the finding is not incremental. The `op3-repair-report-p09.md`
repair is absent from this repository.**

Of the eight numbered items `op3-repair-report-p09.md`'s SELF-AUDIT claims as "done,
mutation-verified" — three PRD §12.2 Safety rows, fire-time-authorization test coverage, the
abort-guard scheduler fix, `compute_full_taint`, the tie-break sentinel unification, and the
RNG-determinism gap closure — **none of the eight is present in the source tree this audit was
asked to read.** The code and tests in `src/agentdx/runtime/faults/`, `tests/unit/faults/`,
`tests/integration/faults/`, `src/agentdx/runtime/scheduler.py`'s abort-guard handling, and
`AGENTS.md`/`.importlinter` are, on every point checked, **byte-for-byte identical in
substance** to what the *first* audit (`op2-audit-p09.md`) found and rejected. Every mutation
that first audit used to prove a gap (delete the four `safety.reauthorize()` call sites; rig
`FaultRandomStream` to explode if drawn; widen `BlastRadius.contains` for `TOOL`) reproduces
the identical result in this session, against the identical, unmodified files.

**One piece of the repair did land and holds up under live re-test: D-49's schema migration**
(`SCHEMA_VERSION` 1→2, `"aborted_guard"` added to `run_end.status`'s enum,
`migrate_v1_to_v2` wired into `decode_event`). That work was done in a separate session
(the D-45 addendum) and is genuinely present, genuinely load-bearing, and mutation-verified
fresh in this audit (Finding #7 / "what holds up"). Everything from the main
`op3-repair-report-p09.md` pass is not.

A `p09_faults_delta.tar.gz` bundle sitting in `_to_delete/` at the repo root — file list
matches the OP-3 deliverable set exactly — is **byte-identical to the current, unrepaired
tree** on every `runtime/faults/*.py` file, which rules out "the fix is sitting in a tarball
nobody applied" as the explanation. Combined with `op3-repair-report-p09.md`'s own closing
line ("I could not sync it to your device this turn because the desktop bridge reports 'not
connected'... I can send them as a tarball now if you'd rather pull them manually") and this
repo's single squashed git commit (`git log` shows exactly two commits total:
`988aa74` P01 scaffold and `1b22884` "P08+P09+P10+P11... catch-up build"), the most likely
explanation is that the repair was built and verified in a session whose changes never
reached the tree that got committed into this repository — the same failure mode CONTEXT.md's
own D-64 already named for P15–17's 129 files. This audit cannot confirm that mechanism; it
can only confirm, with certainty, that the *artifact* — the actual code the ledger says exists
— does not.

---

## FINDING #1 (CRITICAL) — PRD §12.2's `agent_crash` Safety row ("cannot crash the last live
agent unless `allow_total_failure: true`") is unimplemented; a single-agent run crashes its
only agent unconditionally

PRD §12.2, line 2015: `Safety | Cannot crash the last live agent unless allow_total_failure:
true`. `CONTEXT.md` §14 (handoff brief, before P09 ever started) named this explicitly as a
P09 precondition, not a `scenario/` validation gap.

```
$ grep -rn "allow_total_failure\|last_live\|_would_leave_no_live_agent\|last live" \
  src/agentdx/runtime/faults/*.py docs/chaos-safety.md tests/unit/faults/*.py \
  tests/integration/faults/*.py
(no output, exit 1)
```

Live repro — a scenario with exactly one agent, `allow_total_failure` never set (so it
resolves `None`/falsy), crashed via the real `CrashInjector` against a real `Scheduler`:

```
allow_total_failure param as resolved: None
solo agent's own body ran? []
crashed_agents: {'solo': None}
RESULT: the ONLY agent in the run was crashed with no allow_total_failure:true anywhere.
```

`process.py::CrashInjector._due_fault`/`_crash` contain no notion of "how many agents are
still live" at all — `_crash_via_coro_swap`/`pre_yield` unconditionally swap in the crashing
coroutine the instant the trigger is due. `op3-repair-report-p09.md` line 11 claims
`CrashInjector._due_fault`/`_would_leave_no_live_agent` "now enforce it as a silent
skip-and-retry" — `_would_leave_no_live_agent` does not exist anywhere in this file or this
repository (`grep -rn "_would_leave_no_live_agent" .` returns nothing).

**Fix direction:** exactly what the first audit and the (unlanded) repair both already
specified — `CrashInjector` needs a live count of non-crashed agents in the run (tracked via
`pre_schedule`'s `runnable` list plus `_crashed_agents`, or an explicit `known_agents` set
threaded in at construction) and `_due_fault`/`_crash_via_coro_swap`/`pre_yield` must skip
(not fire) a due `agent_crash` whose target would leave zero live agents, unless
`armed.decl.params.get("allow_total_failure")` is `True`. Add a regression test with exactly
one agent and `allow_total_failure` unset/false/true (three cases) before the fix.

---

## FINDING #2 (CRITICAL) — PRD §12.2's `message_drop` Safety row ("cannot drop a `run_end`
control message") and `latency`'s Safety row ("bounded by `max_virtual_duration`") are both
still unimplemented and structurally unexpressible at the current call signatures

PRD §12.2, line 2001: `Safety | Bounded by max_virtual_duration | Cannot drop a run_end
control message`.

```
$ python3 -c "
import inspect
from agentdx.runtime.faults.transport import TransportFaultInjector
print(inspect.signature(TransportFaultInjector.decide_drop))
print(inspect.signature(TransportFaultInjector.__init__))
"
(self, *, edge: 'str', virtual_ts_ms: 'int', message_count: 'int | None' = None) -> 'DropDecision'
(self, *, registry: 'FaultRegistry', seed: 'int', stamp: ..., taint: 'FaultTaintTracker') -> 'None'
```

`decide_drop` has no message-identity parameter at all — there is no way for a caller to tell
it "this delivery carries `run_end`," so the rule cannot be enforced even by a conscientious
caller. `TransportFaultInjector.__init__` has no `max_virtual_duration_ms` parameter — nothing
in this class's construction or `decide_latency` consults any duration budget.

Live repro of the unbounded-latency gap, a `degrade`-pattern fault with `delay_ms=5000`,
called six times:

```
call 0: extra_delay_ms=5000
call 1: extra_delay_ms=10000
call 2: extra_delay_ms=15000
call 3: extra_delay_ms=20000
call 4: extra_delay_ms=25000
call 5: extra_delay_ms=30000
RESULT: delay grows without any ceiling
```

`AbortGuardMonitor.max_virtual_duration_ms` exists and is wired into `pre_schedule` (it trips
*after* virtual time has already advanced past the budget), which is a different guarantee
from the PRD's "bounded" — the fault itself must not propose a delay that blows the budget in
the first place.

**Fix direction:** `decide_drop` needs a `carries_run_end: bool = False` (or equivalent
message-kind marker) keyword and an unconditional early return (`dropped=False`) when it is
`True`, before any trigger/probability evaluation — the caller (harness, and eventually the
SDK message-delivery wrapper) is responsible for knowing whether the message it is about to
deliver *is* `run_end`. `decide_latency`/`TransportFaultInjector.__init__` need an optional
`max_virtual_duration_ms` the delay calculation clamps against (`min(applied,
max(0, max_virtual_duration_ms - virtual_ts_ms))` or equivalent). Neither of these is
implemented, tested, or declared as a gap in `docs/chaos-safety.md`'s "Known gaps" list
(checked — line 316 onward, 8 items, none of the three Safety rows appear).

---

## FINDING #3 (CRITICAL) — I12's fire-time defence-in-depth layer (`safety.reauthorize`) has
zero discriminating test coverage; all four call sites are deletable with the entire suite green

CONTEXT.md §2 names I12's enforcement mechanism as "Scenario validation (`E-SCEN-004`) +
**runtime re-check** (`E-CHAOS-001`)." `safety.py`'s own module docstring (lines 6–12) states
`reauthorize` "is not a wrapper an execution module might forget to call... If a future
fault-class module skipped it, that is a code-review-visible omission... not a
silently-widened blast radius."

Live mutation, this session — all four production call sites removed (`process.py:239`,
`transport.py:175`, `transport.py:228`, `dependency.py:128`):

```
$ sed -i 's/^\( *\)safety.reauthorize(armed, self._registry.blast_radius)$/\1pass  # MUTATED/' \
    src/agentdx/runtime/faults/{process,transport,dependency}.py
$ pytest tests/unit/faults/ tests/integration/faults/ \
    --deselect .../test_100_runs_..._fresh_processes -q
........................................................................ [ 77%]
.....................                                                    [100%]
```

93/93 collected (non-subprocess) tests pass with the entire runtime re-check layer physically
absent. Files restored from backup immediately after; `git diff --stat` confirmed clean.

`tests/integration/faults/test_safety_suite.py` — the file whose own docstring claims to be
"the gate: the chaos safety architecture... actually refuses what it says it refuses" — has
exactly the same shape the first audit described: its one fire-time test
(`test_unauthorized_target_is_also_refused_by_the_runtime_defence_in_depth_check`) calls
`safety.reauthorize(armed, blast_radius)` **directly**, never through a real injector
encountering a blast radius narrowed after arming. No test in `test_process.py`,
`test_transport.py`, or `test_dependency.py` puts a real `CrashInjector`/
`TransportFaultInjector`/`DependencyFaultInjector` in front of an out-of-radius target.
`op3-repair-report-p09.md` line 14 claims "new tests for `CrashInjector`,
`TransportFaultInjector`..., and `DependencyFaultInjector`: arm inside the blast radius,
narrow it after arming, fire, assert `safety.ChaosAuthorizationError`" — none of these tests
exist in any of the three files.

Second live mutation, `BlastRadius.contains`'s `TOOL` branch forced to always authorize:

```
if kind is TargetKind.TOOL:
    return True  # MUTATED probe
```
```
$ pytest tests/unit/faults/ tests/integration/faults/ --deselect ... -q
........................................................................ [ 77%]
.....................                                                    [100%]
```

Still green. `grep -rn "TargetKind\.(TOOL|EDGE|PROVIDER)\|\.contains(" tests/**/faults/*.py`
confirms `.contains()` is exercised only for `STATE_KEY`, `AGENT`, and the `universal=True`
shortcut — never `TOOL`, `EDGE`, or `PROVIDER` — identical to the first audit's finding.

**Fix direction:** unchanged from the original audit's own recommendation — one integration
test per fault class that arms inside the radius, mutates `registry.blast_radius` to exclude
the target, fires, and asserts `safety.ChaosAuthorizationError`; a `BlastRadius.contains`
parametrization over all five `TargetKind` members × (in-radius, out-of-radius). Acceptance:
the two mutations above must both go red.

---

## FINDING #4 (HIGH) — The "determinism with faults" gate never draws from `FaultRandomStream`;
a `% 1000` → `% 100` inversion in the probability modulus is undetected by any test

`tests/integration/faults/test_determinism_with_faults.py`'s own docstring (lines 8–12, still
present, unedited) claims: "If arming `FaultRandomStream`... introduced a single
non-deterministic read..., it would show up here as a hash mismatch." Live probe, rigging the
stream to explode the instant it is drawn:

```python
# triggers.py:62 FaultRandomStream.next_permille
-        self._counter += 1
+        raise AssertionError("STREAM WAS DRAWN")  # MUTATED probe
```
```
$ pytest tests/integration/faults/test_determinism_with_faults.py \
    tests/integration/faults/test_gate_g4.py -q \
    --deselect .../test_100_runs_..._fresh_processes
...                                                                      [100%]
```

All three (non-subprocess) tests pass with the stream primed to raise on its very first draw.
The harness arms exactly one `agent_crash`, a fault class with no `PROBABILITY` path, so this
gate structurally cannot exercise the RNG.

`op3-repair-report-p09.md` line 18 claims a "hardcoded-reference test (`seeded_stream(42)`'s
first 8 values...)" and "a new `test_message_drop_determinism.py`." Neither exists:

```
$ grep -rn "hardcoded_reference\|seeded_stream(42)" tests/
(no output)
$ find tests -iname "*message_drop_determinism*"
(no output)
```

`tests/unit/faults/test_triggers.py::test_probability_trigger_matches_stream_draw_exactly`
is unchanged from the first audit's own quoted text — both sides of its assertion call the
same production `should_fire`/`next_permille` path, so it proves one draw is consumed per
call and nothing about the values, and would not catch a `% 1000` → `% 100` modulus bug
(confirmed structurally identical to `op2-audit-p09.md` §4 Test B; not re-mutated live this
session since the first audit's own reproduction already stands and the file is unedited).

**Fix direction:** unchanged from the original — pin `seeded_stream(42)`'s first N draws as
literal integers computed independently (not via the production function), and add a
determinism harness that arms a `PROBABILITY`-triggered `message_drop`/`tool_failure` and
checks both the canonical hash *and* the literal drop/no-drop bit sequence across 100 replays.

---

## FINDING #5 (HIGH) — Abort-guard trips still land the run in `RunState.FAILED`, never
`RunState.ABORTED_GUARD`, despite the schema now legally supporting the status and the ledger
claiming this was fixed

PRD §13.6, line 2181: "On trip: the injector disarms, in-flight tasks are cancelled, the log
is sealed with `run_end.status = aborted_guard`, and the partial log is retained and
analysable."

Live repro — a real `Scheduler` + `CrashInjector` with `max_virtual_duration_ms=0`, tripped
by a `sleep(1)`:

```
raised: [E-GUARD-001] max_virtual_duration_ms: virtual duration 1ms exceeded budget 0ms at step 2
final RunState: RunState.FAILED
Is it ABORTED_GUARD? False
Is it FAILED? True
```

`scheduler.py::Scheduler.run()` (read directly, lines 870–891) has exactly one exception
branch around the scheduler loop — a bare `except BaseException:` that unconditionally
transitions to `RunState.FAILED` and re-raises. There is no `except AbortGuardTripped:`
branch anywhere in the file (`grep -n "AbortGuardTripped" src/agentdx/runtime/scheduler.py`
returns nothing). `safety.py::AbortGuardTripped`'s own docstring (lines 178–189, unedited)
still states the gap in full: "raising here propagates through `Scheduler.run()`'s existing
`except BaseException` handler, which moves the run to `FAILED`, not `ABORTED_GUARD`...
judged out of scope here and recorded as NOT DONE." `docs/chaos-safety.md`'s "Known gaps"
item 3 (line 325) still reads: "`ABORTED_GUARD` is a legal `RunState` nothing transitions
to." `op3-repair-report-p09.md` line 15 claims `Scheduler.run()` "gained a specific `except
AbortGuardTripped` branch" — it did not, in this tree.

Note the injector also does not disarm and in-flight tasks are not cancelled independent of
the state-transition question — the partial-log-retained half of PRD §13.6 does hold
(`sink.events()` is non-empty after the trip, confirmed both in this session's repro and in
the existing `test_a_tripped_abort_guard_stops_the_run_and_the_partial_log_survives`), but
the "injector disarms" / "in-flight tasks are cancelled" clauses are unaddressed by anything
in this file.

**Fix direction:** unchanged from the original audit — `Scheduler.run()` needs an
`except AbortGuardTripped as exc:` branch, ordered before the generic `except BaseException:`,
transitioning to `RunState.ABORTED_GUARD` (already a legal `_LEGAL_TRANSITIONS` target) and
closing every unfinished task's coroutine before re-raising.

---

## FINDING #6 (MEDIUM) — The two independent "earliest fault wins" tie-break implementations
still disagree on their unknown-fault default; `compute_full_taint`/`payload.fault_ids` (PRD
§9.4) is still unimplemented

`taint.py::compute_causal_taint` (line 125): `min(candidates, key=lambda fid:
(injected_at.get(fid, event.seq), fid))` — defaults an unknown fault's "injected at" seq to
**the current event's own seq**.

`taint.py::FaultTaintTracker.resolve` (line 205): `min(candidates, key=lambda fid:
(self.injected_at.get(fid, 1 << 62), fid))` — defaults an unknown fault to **effectively
infinity**.

These produce different tie-break orderings for the same unknown-fault-id edge case (an
unknown fault sorts as "just injected right now" in one implementation and "definitely not
the earliest" in the other). `op3-repair-report-p09.md` line 17 claims "the two independent
'earliest fault wins' implementations now share one `_UNKNOWN_INJECTION_SENTINEL` constant" —
no such name exists anywhere in the repository (`grep -rn "_UNKNOWN_INJECTION_SENTINEL" .`
returns nothing), and `taint.py`'s `__all__` (line 251) is unchanged from three entries
(`FaultTaintTracker`, `compute_causal_taint`, `taint_summary`) — no `compute_full_taint`.

PRD §9.4's closing sentence ("`payload.fault_ids` holds the full set") remains unimplemented
by any name; `grep -rn "fault_ids" src/ tests/` returns only unrelated matches (`FaultRegistry`
internals, not a `fault_ids` field or function).

**Fix direction:** unchanged from `op3-repair-report-p09.md`'s own (unlanded) design — a
single named sentinel constant shared by both call sites, and `compute_full_taint` as an
offline pure function over a sealed log returning the complete contributing-fault-id set per
`seq` (D-46's own already-approved design, never implemented against this tree).

---

## FINDING #7 (POSITIVE CONTROL — holds up) — D-49's schema migration
(`SCHEMA_VERSION` 1→2, `"aborted_guard"` enum member, `migrate_v1_to_v2`) is real, wired at
the correct boundary, and mutation-verified

Unlike every other claimed repair item, this one is genuinely present:

```
$ grep -n "SCHEMA_VERSION\|aborted_guard" src/agentdx/events/schema.py
50:SCHEMA_VERSION: Final = 2
536:  enum=frozenset({"complete", "failed", "aborted", "timeout", "aborted_guard"}),
```

`events/migrations/__init__.py` has a real, non-empty `MIGRATIONS` registry
(`{1: _migrate_v1_to_v2}`), and `events/canonical.py::decode_event` (line 424) calls
`migrations.migrate(raw, to_version=SCHEMA_VERSION)` before constructing the `Event`, ahead of
`check_structural`'s strict `E-EVENT-008` version check. Live mutation this session — bypassed
the migrate call (`migrated: object = raw`) and re-ran the three tests the addendum's own
report predicted would fail:

```
FAILED tests/unit/events/test_golden_log.py::test_the_fixture_passes_every_validation_layer
FAILED tests/unit/events/test_golden_log.py::test_the_canonical_hash_is_pinned
FAILED tests/unit/events/test_golden_log.py::test_the_fixture_is_deterministic_under_reserialisation
```

Exactly the 3 failures predicted, restored, re-verified green. All four committed golden
fixtures remain byte-for-byte unchanged (confirmed no `regenerate_all()` call site exists and
the fixtures' mtimes predate this session entirely). This piece of work should not be
re-litigated by a future audit absent new evidence — it is real and it holds.

**Caveat, correctly disclosed by the addendum itself and still true**: nothing in this build
sets a real run's `run_end.payload.status` to `"aborted_guard"` — that is Finding #5 above,
the production-wiring half, which the schema migration alone cannot close.

---

## What was checked and holds up

- **D-49's migration** (Finding #7) — real, wired correctly, mutation-verified fresh.
- **The MVP fault-type gate** — `MVP_FAULT_TYPES` (4 members) is still enforced structurally
  in `registry.py`; a 5th/6th fault type still raises `FaultNotImplementedError`
  (`E-CHAOS-002`) before reaching any fault-class module. Unchanged, correct.
- **`E-CHAOS-002`/`E-CHAOS-003`** — both still present, both still correctly declared via D-48
  (retroactive §9 row from the first repair, which *did* land — this was a docs-only ledger
  edit, consistent with the pattern that ledger-only changes survived while code changes did
  not).
- **PRD rules 1 and 2 of §9.4's taint definition** — `compute_causal_taint` and
  `FaultTaintTracker` both implement "directly produced" and "inherited from causal_parents"
  correctly; not re-mutated this session since the first audit's own mutation evidence
  (M5/M6 both red) is against unchanged code and stands.
- **`ruff check`, `ruff format --check`** — clean, run for real this session, against
  `runtime/faults/`, its tests, and the touched `events/` files.
- **`mypy --strict`** (scoped to `runtime/faults/*.py`, working around the sandbox's
  `.mypy_cache` permission issue with `--cache-dir=/tmp/mypycache`) — `Success: no issues
  found in 10 source files`, run for real.
- **`lint-imports`** — `Contracts: 10 kept, 0 broken`, run for real; the `runtime → scenario`
  import remains sanctioned (ADR-013 is recorded in CONTEXT.md §4/§8 and the contract's
  `forbidden_modules` list genuinely never named `scenario`, so nothing regressed here even
  though the `.importlinter` file's own explanatory comment — see Finding below — was never
  added).
- **I2 (append-only), I3 (analysis purity), I7 (offline), I13 (no model in path)** — all held,
  same mechanism the first audit verified (no second write path, `lint-imports` clean,
  no network/model imports under `runtime/faults/`); not independently re-tested by mutation
  this session since nothing in the diff (there is no diff) touches these paths.
- **`docs/chaos-safety.md`'s "Known gaps" section is internally honest** — it still correctly
  lists the `ABORTED_GUARD`/wiring gap (item 3) and the interception-point gaps (item 2) that
  match what this audit found live. The doc was simply never updated to add the three Safety
  rows, `compute_full_taint`, or `fault_summary` persistence as *additional* declared gaps —
  but it does not misrepresent what it does cover.

## Additional ledger-vs-repository discrepancies found (documentation-only, not re-litigating
code findings already covered above)

- **`AGENTS.md` §4.1 has no 5th determinism exemption clause.** ADR-014 (CONTEXT.md §8) claims
  "`AGENTS.md` §4.1 documents clause 5 in full" — the file (read in full, 130 lines) still
  lists exactly 4 sanctioned exceptions, unchanged. `process.py`'s two `wall_time()` reads at
  `pre_schedule` (lines 263, 268) — which branch run-aborting control flow, not merely
  populate a volatile field — remain undeclared and unexempted (`grep -n "determinism-exempt"
  src/agentdx/runtime/faults/process.py` returns nothing), a live, still-open tripwire-2 gap.
- **`.importlinter`'s `runtime-executes-only` contract comment is unchanged.** Line 48 still
  reads `"May import: events, store."` with no mention of `scenario` or ADR-013, despite
  `op3-repair-report-p09.md` line 19 and CONTEXT.md's ADR-013 both claiming this comment was
  added.
- **Test file counts are identical, not increased, to the pre-repair state.** `--collect-only`
  against `tests/unit/faults/` + `tests/integration/faults/` today: `dependency 8, process 7,
  registry 14, safety 17, taint 11, transport 9, triggers 13` (unit, 79) + `test_crash_retry_
  cascade 3, test_determinism_with_faults 2, test_fault_taint_causality 2, test_gate_g4 2,
  test_safety_suite 6` (integration, 15) = **94 total** — the exact same per-file breakdown
  and total the *first* audit's H2 finding quoted as the *pre-repair, wrong-count* baseline
  ("CONTEXT.md says 91; actual 79" unit, 94 total unit+integration). `op3-repair-report-p09.md`
  claims 21 new tests landed (1872 vs 1851 project-wide). None of the 15 test-file additions
  it describes for this module are present.
- **CONTEXT.md §5 row 9 and §13's session-log entry both narrate the repair as done**,
  including specific numbers ("1872/1872 pytest pass," "12 pre-existing ruff I001 import-order
  violations... fixed") that do not describe the artifacts in this tree. This is the load-
  bearing risk for any future session: reading the ledger alone, without re-deriving from the
  actual files (as this audit's own briefing instructed), would produce a confidently wrong
  picture of this module's state.

---

## NOT DONE / RISKS — honest account of what this audit did not verify

1. **The mechanism by which the repair was lost is not confirmed, only inferred.** The
   evidence (single squashed git history, the repair report's own "bridge not connected" note,
   a byte-identical unapplied delta tarball in `_to_delete/`) is consistent with a sync failure
   between an isolated build sandbox and this persistent checkout, matching D-64's precedent
   for P15–17. This audit did not have access to whatever session actually ran the OP-3 repair
   and cannot confirm the mechanism beyond what the filesystem evidence supports.
2. **`test_determinism_with_faults.py`'s `% 1000` → `% 100` mutation was not re-run live this
   session** (Finding #4's second half) — the file is byte-for-byte unchanged from the first
   audit's own read, so its own reproduction (`op2-audit-p09.md` §4 Test B) was trusted rather
   than re-executed, to conserve session budget for mutations against code the first audit had
   not already exhaustively probed. If a future session wants first-hand confirmation, the
   original audit's exact repro steps apply unmodified.
3. **`fault_summary` (PRD §12.5) persistence** — still computed (`FaultRegistry.summary()`)
   but not persisted anywhere; unchanged from D-47, not this module's gap alone (no `RunHost`
   call site exists to seal a run and write it), not re-litigated as a new finding here.
4. **`check_determinism_hygiene.py` could not be scoped to just this module** — it always
   scans the full `src/agentdx/` tree and fails on `api/models.py`'s PEP 695 syntax
   (unparseable under this sandbox's Python 3.10), a pre-existing, already-declared D-66 gap,
   not a P09-specific finding. `ruff`/`mypy --strict`/`lint-imports` were run scoped instead
   and are clean, which is the strongest static signal available in this sandbox.
5. **`docs/chaos-safety.md`'s three false "pasted" claims and "91 tests" count (H1/H2 from the
   first audit) are still present, unchanged** — checked (`grep -n "pasted"` still hits, the
   "Gates and how to reproduce them" section still contains only commands, no output; `tests/
   unit/faults/` is 79, not 91) but not re-quoted as a new numbered finding since it is the
   identical, unrepaired defect the first audit already fully documented under its own H1/H2.
6. **This audit changed no code.** Every mutation described above was applied via `cp` backup
   → edit → test → restore-from-backup, verified clean via `git status --short` /
   `git diff --stat` after each one and once more at the end of the session. The one pre-
   existing untracked file pair (`_smoke_baseline.py`, `_smoke_step2.py`) predates this audit
   and was not touched.
