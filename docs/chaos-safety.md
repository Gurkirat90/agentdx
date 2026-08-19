# Chaos safety architecture (P09)

This document is the prose companion to `src/agentdx/runtime/faults/` — every `_DOCS`-anchored
reference (`docs/chaos-safety.md#e-chaos-001`, etc.) and every `§"..."` reference in that
package's docstrings points at a section here. It is written for someone who has not read the
code: what exists, what was deliberately deferred, what a review should look at twice, and how
to reproduce every gate this build claims to pass.

Scope: the fault injection engine and chaos safety architecture (mission P09). The scheduler,
event schema, and scenario-resolution layers this package depends on are P02/P06/P08 and are
described only to the extent this package's own decisions depend on them.

## Contents

- [MVP fault set](#mvp-fault-set)
- [Interception point mapping](#interception-point-mapping)
- [The authorization model — two layers](#the-authorization-model-two-layers)
  - [E-CHAOS-001](#e-chaos-001)
  - [E-CHAOS-002](#e-chaos-002)
  - [E-CHAOS-003](#e-chaos-003)
- [Steady-state hypothesis](#steady-state-hypothesis)
- [Abort guards](#e-guard-001)
  - [Abort guard wiring](#abort-guard-wiring)
  - [Malformed guard](#malformed-guard)
- [Wiring the taint tracker](#wiring-the-taint-tracker)
- [The scheduler.py deviation](#the-scheduler-py-deviation)
- [Declared vs. linear-fallback causal parents](#declared-vs-linear-fallback-causal-parents)
- [Restart](#restart)
- [Restart and rule 3](#restart-and-rule-3)
- [Gates and how to reproduce them](#gates-and-how-to-reproduce-them)
- [Known gaps](#known-gaps)

## MVP fault set

CONTEXT.md §3 locks this build's fault set to four PRD §12.2 fault types, all tier P0:
`latency`, `agent_crash`, `message_drop`, `tool_failure`. The other six PRD §12.2 fault types
are tier P1 and are explicitly **not implemented** in this build — each is deferred with its own
reason, not silently dropped:

| Fault | Module | Why deferred |
|---|---|---|
| `message_reorder` | `transport.py` (not implemented) | P1, out of `MVP_FAULT_TYPES` — no execution logic beyond the tier gate itself. |
| `message_duplicate` | `transport.py` (not implemented) | Same. |
| `agent_slow` | `process.py` (not implemented) | Same. |
| `rate_limit` | `dependency.py` (not implemented) | Same. |
| `byzantine` | `semantic.py` (declaration-only module) | P1, **and** a genuine architectural gap beyond the tier: PRD §12.2 requires its output come "only from a declared pool," and no fixture-level concept of a "declared response pool" exists anywhere in this build (P08 or earlier) for this module to load from. Implementing it would mean inventing that schema silently, which the mission did not ask for. |
| `state_corrupt` | `state.py` (declaration-only module) | P1, **and** no interception point: PRD §12.1's diagram never names a hook for a state write (`pre_state_write` appears only in prose, not the diagram), and `agentdx.state()` (the SDK's shared-state primitive) has no hook a fault could attach to without an `sdk/generic.py` (P04) change. |

Every deferred type is also **structurally unreachable**, not just undocumented:
`FaultRegistry.from_resolved_scenario` raises `FaultNotImplementedError` (`E-CHAOS-002`) for any
fault type outside `MVP_FAULT_TYPES` at registry-construction time, before any per-fault-class
module ever sees an `ArmedFault` for it. A scenario that declares `byzantine` or `state_corrupt`
fails loudly, every time — it never silently no-ops (PRD §12.5's own warning against that
failure mode).

## Interception point mapping

PRD §12.1 names seven interception points a fault engine needs: `pre_send`, `pre_deliver`,
`pre_resume`, `pre_tool`, `pre_llm`, `pre_state_write`, and (in prose only) a retry-span point.
The fixed P06 `Scheduler` this build integrates with exposes exactly three hook methods:
`pre_schedule(step, runnable)`, `pre_yield(task_id, reason)`, `on_task_done(task_id, exception)`
— none of which is named after a PRD §12.1 point directly.

| PRD §12.1 point | MVP fault(s) that need it | Wired in this build? |
|---|---|---|
| `pre_resume` | `agent_crash` | **Yes** — via `pre_schedule` (a not-yet-started task) and `pre_yield` (a mid-flight task), the two fixed hooks close enough to `pre_resume`'s intent. See `process.CrashInjector`'s module docstring for the exact mapping. |
| `pre_send` / `pre_deliver` | `latency`, `message_drop` | **No.** Nothing in the fixed scheduler or SDK suspends through `Scheduler.yield_point` (or any other hook) when a message is sent or delivered between agents — a targeted grep of `sdk/generic.py` and `sdk/providers/openai_compatible.py` found `yield_point` called from exactly one place, around the LLM call. `TransportFaultInjector.decide_latency`/`decide_drop` are full, correct, pure decision logic that a caller must invoke manually at its own synthetic "message about to be delivered on edge E" site — today, only `tests/integration/faults/`'s own harness does this. |
| `pre_tool` | `tool_failure` | **No.** Same gap, same grep result. `DependencyFaultInjector.decide_tool_call` is the same shape: full decision logic, no live call site. |
| `pre_llm` | (none in MVP — `byzantine` would need it, deferred) | N/A for this build's MVP set. |
| `pre_state_write` | (none in MVP — `state_corrupt` would need it, deferred) | N/A for this build's MVP set. |

**Why this isn't a blocker for the MVP set specifically.** `agent_crash` is the one MVP fault
whose PRD interception point has a real, live counterpart in the fixed scheduler surface — so it
is the one fault class genuinely wired end-to-end through a real `Scheduler` run (see
`tests/unit/faults/test_process.py`, `tests/integration/faults/`). `latency`/`message_drop`/
`tool_failure` are complete, tested, schema-correct decision engines with no live production
call site — the honest state of a build where P02/P06 fixed the scheduler surface before P09
existed, and closing this gap is an `sdk/generic.py` (P04) change outside P09's DELIVERABLES.

## The authorization model — two layers

PRD §13.4 point 2: "Enforced at two layers: validation ... and runtime (`should_fire`
re-checks; a violation raises `E-CHAOS-001` and aborts the run — a defence-in-depth check that
should be unreachable)." This build has two genuinely distinct classes named
`ChaosAuthorizationError`, one per layer — not a duplication bug:

- **Arm time** — `registry.ChaosAuthorizationError` (`ChaosSafetyError` subclass), raised by
  `FaultRegistry.from_resolved_scenario`. This is the structural gate: is this scenario even
  allowed to declare this fault at all? Two sub-checks, both `E-CHAOS-001`:
  - A user-graph scenario (`is_fixture_target=False`) that declares any fault must have both
    `chaos_opt_in: true` **and** a non-empty `blast_radius:` section (PRD §13.3/§13.10). Missing
    either raises with a combined message naming both requirements.
  - Each fault entry's own target must already be inside the resolved blast radius at arm time.
    A fixture target with no `blast_radius:` declared gets `universal=True` (PRD §13.3's
    fixture default, "everything in the fixture") — a user-graph target never does.
- **Fire time** — `safety.ChaosAuthorizationError` (`RuntimeError` subclass), raised by
  `safety.reauthorize`, called by every fault-class execution module immediately before it
  applies an effect (crashing a task, dropping a message, failing a tool call), never once at
  construction and trusted for the fault's whole life. PRD §13.4 calls this "a defence-in-depth
  check that should be unreachable" given arm-time already gates the same rule — reaching it in
  practice would mean a fault's target resolved differently at fire time than at arm time, or a
  defect in `registry.py`.

`tests/integration/faults/test_safety_suite.py` exercises both layers end to end, with actual
raised-error text pasted in [Gates and how to reproduce them](#gates-and-how-to-reproduce-them).

### E-CHAOS-001

Blast-radius / opt-in authorization failure. See
[The authorization model](#the-authorization-model-two-layers) above for both raise sites and
both classes that carry this code.

### E-CHAOS-002

`FaultNotImplementedError` (`registry.py`) — a declared fault's type is outside
`MVP_FAULT_TYPES`. See [MVP fault set](#mvp-fault-set).

### E-CHAOS-003

`ChaosSafetyError` (bare, via the `registry._e_chaos_003` helper) — a fault entry is malformed
in a way `scenario.validate`'s own structural checks (unknown fault type, missing target field,
wrong value type, no recognised trigger field) should already have caught. Defence-in-depth
only, same posture as `E-CHAOS-001`'s fire-time layer: reaching it means scenario validation
(P08) had a gap, not that this module invented a new rule.

## Steady-state hypothesis

PRD §12.4/§13.5: "you cannot measure deviation from a steady state you never had." A scenario's
`hypothesis:` section (`task_success`, `p95_virtual_duration_ms`, `max_token_spend`, each a raw
`"<op> <value>"` comparison string) is checked against baseline-phase metrics *before* any fault
ever arms — `SteadyStateHypothesis.check(metrics, phase="baseline")` raises `AbortPrecondition`
if any declared metric either fails its comparison or was never measured at all (PRD §13.5 gives
no "metric not measured, skip the check" escape hatch). A baseline-phase violation aborts the
whole experiment before a single `fault_injected` event is ever written. The same `check` method
is reused for the fault-phase post-hoc delta (PRD §12.4's lifecycle table) — only the `phase`
string differs; the caller (the scenario execution lifecycle, out of P09's scope — no `RunHost`
exists yet) decides which phase's violation is fatal vs. merely reported.

## E-GUARD-001

PRD §13.6's six abort guards (`max_virtual_duration_ms`, `max_tokens`, `max_retries`,
`max_wall_duration_s`, `max_events`, `max_llm_calls`), implemented by `safety.AbortGuardMonitor`.
Each `observe_*` method is a pure check-and-report — it never aborts anything itself; a caller
raises `AbortGuardTripped` (`E-GUARD-001`) on the first non-`None` `GuardTrip`.

### Abort guard wiring

Only two of the six guards are evaluated live in this build: `max_virtual_duration_ms` and
`max_wall_duration_s`, both via `observe_step`, called from `CrashInjector.pre_schedule` (the
one interception point with continuous access to every scheduler step). The other four —
`max_tokens`/`max_llm_calls` (`observe_llm_call`, "every `llm_call`"), `max_retries`
(`observe_retry`, "every retry span"), `max_events` (`observe_event_batch`, "every write
batch") — are fully implemented and unit-tested against hand-built call sequences, but nothing
in this build's real scheduler run calls them: the fixed `FaultInjectorHook` surface has no
interception point for an `llm_call`, a retry span, or a write-batch, the same gap
[Interception point mapping](#interception-point-mapping) already describes for
`latency`/`message_drop`/`tool_failure`.

A second, smaller gap: `runtime.scheduler.RunState.ABORTED_GUARD` exists as a legal transition
target from `RUNNING`, but nothing in the fixed scheduler actually transitions to it —
`AbortGuardTripped` propagates through `Scheduler.run()`'s existing `except BaseException`
handler, which moves the run to `FAILED` instead. NFR-13 ("analysable partial log") still holds
— every event up to the trip was already written and flushed — but the terminal `RunState`
value is not PRD-exact. Reaching `ABORTED_GUARD` specifically would need a second, dedicated
`scheduler.py` touch (a public abort method, or a hook return value the scheduler interprets)
beyond the two narrow, justified touches this build already makes (see
[The scheduler.py deviation](#the-scheduler-py-deviation) and
[Declared vs. linear-fallback causal parents](#declared-vs-linear-fallback-causal-parents)) —
judged out of scope and recorded here rather than guessed at with a third unreviewed change.

### Malformed guard

`safety.MalformedGuardError` — defensive only. `AbortGuardMonitor.from_resolved_guards` type/
range-checks every guard value; `scenario.validate`'s `E-SCEN-008` should already have caught a
non-integer guard value before this build ever sees the resolved scenario.

## Wiring the taint tracker

`taint.FaultTaintTracker` is constructed once per run and shared — the same instance is passed
to every fault-class module active in that run (`CrashInjector`, `TransportFaultInjector`,
`DependencyFaultInjector`), so taint resolved by one fault-class module's effect is visible to
another's `causal_parents` lookups within the same run. Two entry points, called from two
different places for a reason (see [The scheduler.py deviation](#the-scheduler-py-deviation)):
`resolve` before an event is stamped (read-only against state from strictly earlier events),
`record` immediately after, so a draft that fails PRD §9.6 validation can never poison the
tracker with a `seq` that was never actually written.

## The scheduler.py deviation

CONTEXT.md D-43: P09 adds exactly one new pair of hook methods to the fixed P06
`FaultInjectorHook` protocol — `fault_id_for(draft, causal_parents)` and
`on_event_stamped(event)` — called from `_SchedulerRecorder.write`, the single call site that
constructs every `Stamp` (Design Constraint 1: still true after this addition). Both are no-ops
on the default hook, so every P06/P07/P08 caller that predates P09 is unaffected. This is the
one narrow, justified `scheduler.py` touch this build makes to wire fault taint through the one
place a `Stamp`'s `fault_id` field (present in the schema since P02, always `None` until this
call site existed) can ever be set.

A second, smaller touch to the same method was necessary once gate G4 testing surfaced the issue
[Declared vs. linear-fallback causal parents](#declared-vs-linear-fallback-causal-parents)
describes — changing *what value* is passed as `fault_id_for`'s `causal_parents` argument, not
adding a new call site or hook.

## Declared vs. linear-fallback causal parents

**This is the most significant correctness issue found during this build, and it was found by
gate G4's own test, not by inspection.**

`Scheduler._causal_parents(seq, causes)` — pre-existing P06 logic, unchanged by this prompt —
returns `sorted(set(causes))` when the caller declared explicit `causes`, and falls back to
`[seq - 1]` (the previous event, linearly) when `causes` is empty. This fallback exists so
*every* event, including scheduler-internal ones like `schedule_decision` (emitted every single
step, always with empty `causes`), has a provable predecessor for the hash-chain/vector-clock
(PRD §9.3/§14.2) — a continuity guarantee, not a causation claim.

The first version of `_SchedulerRecorder.write` fed this exact fallback-inclusive value into the
new `fault_id_for` hook. Gate G4's 20-repeat harness (`tests/integration/faults/_harness.py`,
`kill_reviewer.yaml`-shaped) caught the consequence directly: the `tester` agent's own event —
stamped with no declared `causes`, running concurrently and with no genuine relationship to the
`reviewer` crash — inherited the crash's `fault_id`, purely because it happened to be the next
event in `seq` order. So did every `schedule_decision` event stamped afterward. This is
observably indistinguishable from a time window and contradicts both PRD §9.4's own framing
("computed from the causal graph, not a time window") and the mission's explicit Definition of
Done wording ("a concurrent unrelated branch does not [carry fault_id]").

**The fix.** `_SchedulerRecorder.write` now computes a second value —
`sorted(set(causes)) if causes else []` — and passes *that* to `fault_id_for`, never the
fallback-inclusive `causal` value. `Stamp.causal_parents` (the persisted field, hash-chain and
vector-clock) is completely unaffected — it is still built from the original fallback-inclusive
`causal` value, exactly as before. Only what the fault-taint hook is shown changed. See
`runtime.scheduler._SchedulerRecorder.write`'s own "Deliberately NOT `causal`" comment for the
in-code version of this explanation, and `tests/integration/faults/test_fault_taint_causality.py`
for the dedicated regression test (a two-hop genuinely-declared `causes` chain that correctly
inherits taint, alongside a concurrent bystander event, seq-adjacent to the fault, that
correctly does not).

**A residual limitation this fix cannot fully close.** `taint.compute_causal_taint` — the
*offline* rules-1-and-2 re-deriver, given only a sealed log — has no equivalent fix available:
it reads `Event.causal_parents`, the one persisted `Stamp` field, which does not distinguish
"caller-declared" from "linear-fallback" parents after the fact. On a log containing
fallback-derived `causal_parents` (any event stamped with empty `causes`), `compute_causal_taint`
may attribute rule-2 taint the live `FaultTaintTracker` would not have. Closing this fully would
mean persisting declared and fallback parents as two separate fields — a P02 event-schema
change, out of this prompt's scope. Documented in `taint.py`'s own module docstring; prefer the
live `FaultTaintTracker` over `compute_causal_taint` when precision on this exact question
matters.

## Restart

PRD §12.2: a recoverable `agent_crash` "restarts after `restart_after_ms` with cleared local
context but intact shared state." `CrashInjector` satisfies this by construction, not by
clearing anything: a restarted agent gets a genuinely new `Task`/coroutine (fresh local
variables — nothing carried over), while `agentdx.state()`'s shared registry lives outside any
`Task` entirely and was never touched by the crash. What `CrashInjector` does **not** do:
automatically re-drive the agent's original node function from the top (that needs the SDK/
LangGraph binding this module has no handle on) — `spawn_restart_coro` is a caller-supplied
factory (optional constructor argument; a recoverable crash still fires and is recorded even if
`None` is passed, just with no new task spawned — declared, not silent). The harness or a future
`RunHost` decides what a "restarted `reviewer`" actually re-executes.

## Restart and rule 3

A genuine finding from `tests/integration/faults/test_crash_retry_cascade.py`, not a
hypothesis: PRD §9.4 rule 3 ("the fault_id carried on the agent's context after it observed a
faulted input ... until the task completes") is implemented
(`FaultTaintTracker.mark_agent_tainted`/`clear_agent`), and `CrashInjector._crash` calls
`mark_agent_tainted` for the crashing agent — but it has **zero observable effect for
`agent_crash` specifically**, in this build. `agent_crash` always ends its own task by raising
an exception immediately; `Scheduler._drive_coro`'s `except Exception` clause calls
`on_task_done` (which calls `clear_agent`) synchronously, in the same call stack, with no
`await` in between. So `mark_agent_tainted` is set and cleared back-to-back before any other
code — in particular, before a later-spawned *restarted* task (a new `Task` object, even though
it shares the same `agent_id`) — ever gets a chance to observe it.

This does not mean a restart's events can't carry the crash's taint, only that rule 3 is not the
mechanism that does it here. The mechanism that works: the restart coroutine (caller-supplied —
this module has no opinion on what a restarted agent does) declares an explicit `causes=` edge
back to the crash's `fault_effect` event on its first stamped action. That is rule 2, the
caller's decision to make, exactly as legitimate as any other declared causal edge.

Separately: `TransportFaultInjector`/`DependencyFaultInjector` (the fault classes where rule 3
would plausibly matter most — an agent that catches a tool failure or a delayed message and
keeps running in the *same* task) never call `mark_agent_tainted` at all today, because
`decide_tool_call`/`decide_latency`/`decide_drop` are not even given an `agent_id` to key it by.
Rule 3 is implemented and unit-tested at the `FaultTaintTracker` level (called directly, not
through a real fault firing — see `tests/unit/faults/test_taint.py`), but no fault-class module
in this build currently wires it to have an observable end-to-end effect. Recorded here and in
the closing NOT DONE list rather than silently left for a future reader to rediscover.

## Gates and how to reproduce them

All commands below assume `PYTHONHASHSEED=0` (AGENTS.md's determinism-hygiene requirement,
enforced at runtime by `E-SCHED-004` if unset) and the project virtualenv active.

- **Gate G4** — "killing `reviewer` at `t=3000` reproduces the same failure class and cascade
  shape, 20/20." The literal verification command
  (`agentdx scenario run scenarios/kill_reviewer.yaml --repeat 20`) cannot run: `agentdx
  scenario run` is P17 (CLI, not yet built) and no `RunHost` exists to wire a real graph
  through the scheduler. Reproduced instead against a real `Scheduler` + `CrashInjector` at the
  scenario file's own seed and crash timestamp:
  `pytest tests/integration/faults/test_gate_g4.py -q -s`
- **Fault-taint causality** (dedicated, per Definition of Done's exact wording):
  `pytest tests/integration/faults/test_fault_taint_causality.py -q -s`
- **Crash-and-retry cascade demo**:
  `pytest tests/integration/faults/test_crash_retry_cascade.py -q -s`
- **Safety suite** (unauthorized target, missing blast radius, missing/unmeasured hypothesis
  metric, tripped abort guard):
  `pytest tests/integration/faults/test_safety_suite.py -q -s`
- **Determinism regression with faults enabled** (gate G3's own 100-run, 10-fresh-process bar,
  re-checked against a scenario where a real fault fires on every run):
  `pytest tests/integration/faults/test_determinism_with_faults.py -q -s`
- **Full unit suite for this package**: `pytest tests/unit/faults -q`
- **Static gates**: `ruff check src tests`, `ruff format --check src tests`, `mypy --strict`,
  `lint-imports`, `python scripts/check_determinism_hygiene.py`.

## Known gaps

Consolidated list — see the mission's own closing NOT DONE/RISKS block for the same items in
that required format:

1. No `RunHost`/CLI exists (P17) — every gate above is demonstrated against a hand-authored
   `Scheduler` harness, the same precedent gate G3 (P06) set.
2. `latency`/`message_drop`/`tool_failure` have no live production interception point — see
   [Interception point mapping](#interception-point-mapping).
3. `ABORTED_GUARD` is a legal `RunState` nothing transitions to — see
   [Abort guard wiring](#abort-guard-wiring).
4. Rule 3 (ambient agent-context taint) has no fault-class module in this build that gives it an
   observable end-to-end effect — see [Restart and rule 3](#restart-and-rule-3).
5. `taint.compute_causal_taint` (offline) cannot distinguish declared from linear-fallback
   causal parents on a sealed log the way the live `FaultTaintTracker` can — see
   [Declared vs. linear-fallback causal parents](#declared-vs-linear-fallback-causal-parents).
6. `runtime→scenario` import: `runtime/faults/safety.py` and `registry.py` import
   `agentdx.scenario.schema` (`TargetKind`, `TriggerKind`, `FAULT_CATALOGUE`, `BLAST_RADIUS_KEYS`,
   `parse_comparison`) — permitted by `.importlinter` (`runtime/` may import
   `agentdx.scenario`), not a new exception carved out for this prompt.
7. `scenario/validate.py` has three pre-existing determinism-hygiene violations (`self.agents/
   tools/edges: set[str] = set()`, lines 234-236) predating this prompt and outside its
   DELIVERABLES — left unfixed, flagged rather than silently touched.
8. `byzantine`/`state_corrupt` deferred with reasons beyond the tier gate — see
   [MVP fault set](#mvp-fault-set).
