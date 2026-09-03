# D-62 design — making an agent step a scheduler task

**Status:** design only. No code. **§3's experiment has been run** (2026-09-01) and its
result is recorded there: the framing in `CONTEXT.md` — and in the first revision of this
document — was wrong. Fan-out is not D-62's cause.

Everything here is from reading the shipped code or from that experiment; line references are
real, and every claim is marked as one or the other. §3a (Option D) exists *because* of the
measurement and was not in the original option set.

---

## 1. The problem is bigger than "nothing calls `spawn()`"

D-62 is recorded as: nothing in `sdk/` calls `runtime.scheduler.Scheduler.spawn()`. True —
`grep -rn '\.spawn(' src/agentdx/` returns zero hits against a `spawn` defined at
`runtime/scheduler.py:752`.

But the reason is structural, not an oversight:

**`sdk.generic.Scheduler` — the Protocol the entire SDK programs against — has exactly one
method, `yield_point` (`sdk/generic.py:261-271`). There is no `spawn` on it. The string
"spawn" does not appear in `sdk/generic.py` or `sdk/langgraph.py` at all.**

So the SDK cannot spawn. Its abstraction of "scheduler" has no concept of task creation.
Closing D-62 means widening that Protocol, which is a public-interface change under
`AGENTS.md` §3 and needs an ADR before a line is written.

Second structural fact: `InstrumentedGraph.ainvoke` (`sdk/langgraph.py:813`) delegates
straight to `self._graph.ainvoke(...)`. **LangGraph's Pregel executor owns execution
entirely.** AgentDX instruments *nodes* — `run_node_async` (`:643`) wraps each node and
records reads, writes and spans — but never owns *scheduling*. The scheduler is a parallel
universe that the reference fixtures never enter.

Third: the only `yield_point` calls anywhere in `sdk/` are
`providers/openai_compatible.py:299` and `:308`, around a model call. **The scheduler's
entire visible interleaving space today is "before and after an LLM call."** Not node
boundaries, not state writes.

## 2. How the deadlock actually happens

`_resume_task` (`runtime/scheduler.py`) resumes a task like this:

1. resolve the task's `asyncio.Future`, or `ensure_future(self._drive_coro(task))` on first
   entry;
2. `await self._real_asyncio_sleep(0)` — **exactly one real event-loop tick**;
3. return to `_scheduler_loop`.

A task suspends by awaiting a Future *the scheduler created* (`yield_point` → `RUNNABLE`,
`sleep` → `BLOCKED` + a timer). `_collect_runnable` only collects `PENDING` and `RUNNABLE`.

So a task that suspends on anything **else** stays in `RUNNING`. It is not runnable, it has
no timer, and `_has_remaining_tasks()` is still true — `_scheduler_loop` raises
`DeadlockError` (`E-SCHED-003`).

*(An earlier revision said "any real async machinery needing more than one tick". The tick
count is retracted — see §3's retraction. The measured condition is "a suspension whose
resolution requires the event loop to run another task".)*

That is exactly the observed failure: `{t_r_50f3b6_root_0: }` — one root task, **empty**
`wait_reason`, because `wait_reason` is only set by `yield_point`/`sleep` and the root never
reached either.

## 3. The framing correction — SETTLED BY EXPERIMENT, 2026-09-01

`CONTEXT.md` §7/§9, the `Dockerfile` header and this session's own earlier notes all said the
deadlock is caused by **LangGraph's parallel fan-out**. That is **wrong**, and it is now
measured rather than argued.

`tests/integration/runtime/test_d62_suspension_contract.py`, run on Darwin/arm64, CPython
3.12.2:

| Probe | Result |
|---|---|
| root coroutine that never suspends | **completes** |
| root awaiting an already-resolved Future | **completes** |
| root awaiting an `Event` set by another real task | **`DeadlockError`** |
| **single-node sequential LangGraph graph, no fan-out** | **`DeadlockError`** |

**The boundary, stated only as far as it was observed:** the scheduler tolerates `await`, but
not an `await` whose resolution requires the event loop to run *another task*. The first two
probes are what make that precise — without them, "the scheduler rejects suspension" would
have been the obvious and wrong reading.

**A graph with nothing to parallelise cannot be deadlocked by parallelism.** Fan-out is
incidental. D-62 is a suspension-contract problem.

### Retraction: the "one event-loop tick" claim

An earlier revision of this section asserted a **one event-loop tick** budget. `_resume_task`
does grant exactly one `await self._real_asyncio_sleep(0)` — that much is readable — but the
tick *count* was never measured, and the first version of the experiment that claimed to
measure it was broken: its "one tick" control used `ensure_future` + `Event.wait()`, and
`ensure_future` only schedules, so the case always required another task to run and could
never have passed. The control failed, which is what controls are for. **The tick number is
retracted; "requires another task to run" is what the evidence supports.**

## 3a. Option D — the option the experiment suggests, which §4 did not consider

Options A, B and C below were all written on the assumption that D-62 is about *who owns
scheduling*. The measurement points somewhere much smaller.

`_scheduler_loop` raises `DeadlockError` the moment no **scheduler** task is runnable and no
scheduler timer is pending — **while the real event loop may still have pending work**. The
`Event`-setter probe proves this directly: that coroutine completes fine under a plain
`asyncio.run`; the scheduler refuses a suspension that would have resolved.

**Option D: do not declare deadlock while the event loop has pending work.** Before raising,
yield to the real loop and re-check. If the loop makes progress, continue; if nothing is
runnable after a genuine quiesce, *then* it is a deadlock.

- **Gets:** possibly the whole of D-62, in `runtime/scheduler.py`, without touching `sdk/`,
  without widening `sdk.generic.Scheduler`, and without coupling to Pregel internals. Every
  probe above would pass.
- **Costs, and these are the reason this is not a recommendation:** letting the real loop run
  work the scheduler cannot see is exactly what **I1** exists to prevent. It is only safe if
  that work emits no events — true for Pregel's plumbing, *not* guaranteed in general.
  Detecting "the loop has pending work" without depending on CPython internals needs care.
  And a wrong version of this converts a real deadlock into a hang, losing `E-SCHED-003`'s
  diagnostic value.
- **Verdict: needs its own experiment before it is a candidate**, not adoption on the strength
  of being smaller. But it must be on the list, because A and B both pay a large cost to solve
  a problem that may not be the one that exists.

## 4. The original three designs

Written before the experiment. Read §3a first — the measurement may make all three
unnecessary.

### Option A — the scheduler drives Pregel, one superstep at a time

Stop calling `graph.ainvoke()`. Drive LangGraph through its step-level API, executing one
superstep per scheduler step and `spawn()`-ing each parallel branch as its own task.

- **Gets:** real interleaving over the literal fixtures. Race detection, `explore/`'s delay
  schedules and G1/G2 all become true of the shipped fixtures rather than of synthetic
  harnesses (closes D-55 too).
- **Costs:** couples AgentDX to LangGraph's internal step API, which is **not** a stable public
  contract. `CONTEXT.md` §3 locks LangGraph as the framework, but not to a version whose
  internals may be relied upon. Highest-risk option, and the one most likely to break on a
  LangGraph upgrade.

### Option B — node bodies become scheduler tasks

Let Pregel decide *what* runs. Make each instrumented node's body a scheduler task: at
`run_node_async`, `spawn()` the node body and have the Pregel-facing wrapper await a scheduler
Future that the scheduler resolves when it chooses that task.

- **Gets:** the scheduler controls ordering at node granularity, over real fixtures, without
  reaching into Pregel's internals.
- **Costs:** two schedulers coexist, and their interaction has to be reasoned about carefully —
  Pregel still owns superstep barriers. Gives node-level, not statement-level, interleaving,
  so some race schedules remain unreachable. **I1 risk: whether determinism holds depends on
  Pregel's own ordering being deterministic, which is an assumption to verify, not assume.**

### Option C — generic path only, LangGraph observe-only

Widen the Protocol and wire `spawn` for the `@agent`/`@tool` decorator path, which AgentDX
fully controls. Declare LangGraph observe-only.

- **Gets:** small, safe, no framework coupling.
- **Costs:** **does not unblock any gate.** All three reference fixtures are LangGraph graphs,
  so G1, G4, G9 and G10 stay exactly where they are. Only worth it as a deliberate step toward
  rewriting the fixtures on the generic path — which is an ADR-001-sized decision about the
  golden corpora, not a repair.

## 5. What any option must preserve

Non-negotiable, and each needs a test that fails when it is broken:

- **I1** — same seed + same cache + same scenario → byte-identical canonical projection.
  Gate G3 covers this; it must still pass with real fixtures actually executing, which is a
  strictly stronger condition than it faces today.
- **I4 / G2** — `research_fanout` still yields zero race findings, now for real rather than on
  a synthetic harness shaped like it. **This gets harder, not easier**: real execution explores
  schedules the synthetic harness never did. A new false positive here is a genuine finding
  about the detector, not a reason to weaken the gate (`AGENTS.md` §5).
- **The single stamping point** — `Scheduler.stamp` / `recorder.emit` stay the only places a
  `Stamp` is constructed. Spawned tasks must not acquire a second path.
- **D-55** closes only if the literal fixtures are explored. Option B may not fully close it;
  say so rather than quietly claiming it.

## 6. Recommended sequence

**Updated 2026-09-02 (second pass, same day): steps 1-3 below are done.** Option B is chosen
and recorded as **ADR-017** (`CONTEXT.md` §8). What follows is the state as of that decision,
kept rather than rewritten so the reasoning stays visible.

1. ~~Run the §3 experiment.~~ **Done, 2026-09-01/02.** Result: fan-out is not the cause; see
   §3 and D-81.
2. ~~Check Option B's own flagged assumption before writing its ADR.~~ **Done, 2026-09-02.**
   `tests/integration/sdk/test_pregel_reducer_write_order_is_deterministic.py`: against the
   pinned LangGraph (1.2.10), a `research_fanout`-shaped 4-worker fan-out into one
   `operator.add` reducer produced 23 of 24 possible real completion orderings across 80 runs,
   and exactly one final write order every time — `apply_writes` sorts tasks by path before
   folding writes, so the fold order is a fixed function of graph structure, not of real
   completion timing. Option D was not re-examined; it remains exactly where §3a left it,
   needing its own experiment, not chosen here.
3. ~~Pick A, B or D and write the ADR.~~ **Done, 2026-09-02 — Option B, ADR-017.** Rationale
   in the ADR itself: A's cost (coupling to LangGraph's non-public step API) was judged higher
   than B's now-checked assumption; D was not re-litigated, its own unresolved I1 risk stands.
   **Accepted trade, stated plainly:** D-55 is not closed by this choice — only Option A would
   have closed it — because D-55 is P1/cut-safe while G1/G4/G9/G10 are not (`CONTEXT.md` §5).
4. **Next, not yet started:** implement — widen `sdk.generic.Scheduler` with `spawn()`, wire
   `sdk/langgraph.py::run_node_async` to use it. This is its own unit of work, checkpointed
   with the owner before starting rather than folded into the ADR pass, precisely because it
   is the higher-blast-radius half of this decision.
5. **Re-run G3 and G2 first**, before G1 or G4, once built. If real execution breaks
   determinism or produces a false positive on the healthy fixture, that is the finding — and
   both gates are on PRD §44.3's never-waived list.
5. Only then G1, G4, and the demo gates.

## 7. What this does not touch

D-78 (`run_id` collision) is independent and blocks G9 on its own — see `d78-plan.md`.
G10 has a **second** independent failure beyond D-62: the one cold measurement, 181.092 s,
exceeds the 180 s threshold. Closing D-62 does not close G10.

---

## 8. The fan-out dispatch gap (2026-09-03) — found implementing Option B, task #25

**Status: design only again, on one narrow question. Everything else in this section is
built, validated, and additive.** §4-§6 are unaffected: Option B is still the right choice,
`resume_drain_ticks` (§3-era finding) is still correct and unchanged, and nothing here
argues for reopening Option A or D as the *default* path — §8.4 below revisits Option A
only because this section's own finding changes its cost/benefit slightly, not because
anything else about it changed.

### 8.1 Where this picks up

Attempting D-62 Option B's own Step 4 (a real fixture, end-to-end, not the synthetic
sequential test graph `tests/unit/sdk/graphs.py::build_pipeline`) found that **all three**
reference fixtures fan out (`fixtures/code_pipeline/graph.py` itself is
`planner -> {coder, reviewer} -> tester` — this was wrongly believed sequential earlier the
same day) and all three hit a bug `resume_drain_ticks` does not touch: `join`'s ambient
caller-identity resolution collides the moment Pregel dispatches two or more ready nodes
concurrently in one superstep (`PregelRunner.atick`'s `self.submit()` path, one hidden
`asyncio.Task` per node, none of them `spawn`ed by this scheduler — every one inherits the
*same* `SchedTaskContext` its parent had ambient, by ordinary `contextvars` copy-on-`Task`-
creation). A full staff-engineer-style audit of this (delta table, root cause classified
**(c)** — a genuine gap in this document's own §4/§5, silently filled by code reusing
machinery built for the scheduler's single-cooperative-task case) is recorded in
`CONTEXT.md` §13, 2026-09-03. The owner approved building the audit's "candidate 2":
give each `run_node_async` call its own scheduler identity when it needs one, via two new
`Scheduler` methods, `begin_call`/`end_call` (ADR-018).

### 8.2 What's built and validated

`runtime/context.py` gains `bind_task`/`unbind_task` (a raw, non-context-manager pair
alongside the existing `use_task`, needed because `begin_call`/`end_call` straddle a
caller-owned `try`/`finally` around an ordinary function call, not one lexical `with`
block). `runtime/scheduler.py`'s `Scheduler` gains `begin_call`/`end_call` and
`self._identity_owners` (which real `asyncio.Task` legitimately owns each bound identity,
recorded once by `_drive_coro`). `sdk/generic.py` widens the `Scheduler` Protocol to match
(`ImmediateScheduler`'s versions are no-ops — it never had this problem, since its `join`
resolves its target directly from `task_id`, never from an ambient caller identity).
`sdk/langgraph.py::run_node_async` wraps its existing `spawn`/`join` call in
`begin_call`/`end_call`.

**The mechanism, corrected once already, empirically.** A first version minted a fresh
identity on *every* `begin_call`, unconditionally. That broke the sequential case
`resume_drain_ticks` already fixed: Pregel's single-ready-node fast path calls
`run_node_async` **inline** — same real `asyncio.Task` as whichever task is already driving
`graph.ainvoke()` (typically root), no hidden task at all. Minting a fresh identity there
still rebinds the ambient context away from that task, so its own `spawn`/`join` calls
start resolving against the *new* identity instead — and the *original* task's own
`Task.state` is then never touched by anything again, stuck at `RUNNING` forever, invisible
to `_collect_runnable`. Confirmed by trace: `DeadlockError`, only that one task listed,
*before* any concurrently-dispatched sibling ever ran a single line. The fix:
`self._identity_owners` — `begin_call` compares `asyncio.current_task()` (the real task
calling *right now*) against the recorded owner of whatever identity is currently ambient.
Same real task → no-op, return `""`, change nothing (restores the original, working
sequential behaviour exactly). Different real task → mint a fresh identity, as originally
intended.

**Validated two ways.** The full existing suite is unaffected — 2131 passed, 0 failed,
identical to the pre-this-fix baseline (this is purely additive: nothing existing calls
`begin_call`/`end_call` except the one new call site). And empirically, against a harness
built to mirror Pregel's own two dispatch paths directly against the real `Scheduler` (not
`ImmediateScheduler`): a root that runs one sequential node via `begin_call`+`spawn`+`join`
(mirroring the fast path), then fans out into two concurrent, real, raw `asyncio.Task`s
(mirroring `self.submit()`) each independently calling `begin_call`+`spawn`+`join`. The
sequential call correctly took the no-op path. The two concurrent calls correctly minted
distinct identities — no collision, no orphaned Future, no cross-talk between coder's and
reviewer's own bookkeeping.

**This closes the identity collision. It does not, on its own, let a fan-out superstep's
node bodies actually run.** That is §8.3.

### 8.3 What's still open: the dispatch gap itself

**First finding — the spawned bodies are never dispatched at all.** `_scheduler_loop`
drives exactly one task at a time: `while ...: runnable = self._collect_runnable(); chosen
= self._choose(runnable); await self._resume_task(chosen)`. `_resume_task`'s resumption
drain loop (the `resume_drain_ticks` mechanism) grants the *already independently running*
background task (root's own, since its first dispatch) extra real ticks — it does not, and
architecturally cannot without change, drive `_scheduler_loop`'s own *next* iteration,
because that next iteration cannot start until the *current* `await self._resume_task(root)`
call returns. Root's own coroutine, mid-fan-out, is stuck on a raw `asyncio.gather`/
`asyncio.wait` over Pregel's own hidden tasks — nothing about that raw await ever touches
`self._tasks['root'].state`, so it stays `RUNNING` for the *entire* drain window. The two
fanned-out calls' own spawned node-body tasks sit `PENDING` in `self._tasks` the whole
time — real, correctly-identified, entirely inert — because `_scheduler_loop` never gets a
turn to pick them up. Confirmed by trace: root's drain exhausts its full
`resume_drain_ticks` budget and raises `SchedulerError`, having made zero progress on
either spawned body; a probe with a smaller/no prior sequential step instead hits
`_scheduler_loop`'s own `DeadlockError` immediately, before either hidden task runs a
single line.

**Second finding — naively unblocking the parent breaks completion the other way.** A
follow-up attempt had `begin_call` flip the parent (root) to `BLOCKED` while any call
minted against it is outstanding (reference-counted, since a superstep can fan out to more
than two) — this *worked* for dispatch: `_resume_task(root)`'s drain loop correctly exits
the moment root is `BLOCKED`, `_scheduler_loop` regains control, and the two node-body
tasks get dispatched, run, and complete completely normally. Confirmed by trace: both
`join()` calls returned real results, and root's own `asyncio.gather` resolved with both —
**the fan-out itself completed correctly.** The bug is in the other half: `end_call`, once
every sibling reaches it, flipped the parent back to `RUNNABLE` — but root's own real
coroutine was *never* suspended on anything the scheduler tracks in the first place (it's
suspended on Pregel's own `asyncio.gather`, not a `join`/`sleep`/`yield_point` Future); it
keeps running independently the entire time and reaches `DONE` on its own, through the
*existing* `_drive_coro` completion path, whenever its own `await` actually resolves.
Flipping it to `RUNNABLE` makes `_scheduler_loop` try to dispatch it *again* — and
`_resume_task`, finding no stored Future for it, takes the *first-dispatch* branch:
`asyncio.ensure_future(self._drive_coro(task))`, which tries to `await task.coro` on a
coroutine object that is already being (or already was) awaited. Confirmed by trace:
`RuntimeError: coroutine is being awaited already`, arriving *after* root's own
"gather returned" line — i.e., after the real work had already, correctly, finished; the
crash is purely in the redundant re-dispatch.

**The question underneath both.** `_scheduler_loop`'s deadlock check —
`if not runnable: if not self._timers: raise DeadlockError(...)` — is synchronous and
unconditional the moment `_collect_runnable` comes back empty. It has no notion of "a task
that is `BLOCKED`, and so invisible to `_collect_runnable`, may still be independently
progressing right now, on real concurrency (Pregel's own hidden tasks) it does not need the
scheduler's cooperation to finish, and finding out one way or the other costs nothing more
than a real tick." Both halves of this — getting the scheduler's *own* newly-spawned tasks
(the node bodies) dispatched while the parent's drain window is open, and correctly judging
when it is safe to simply leave the parent alone rather than either declaring deadlock too
eagerly or wrongly re-driving it — trace back to this one gap.

### 8.4 Candidate directions — none chosen

**Candidate α — teach the resumption drain loop to also dispatch other runnable tasks.**
On each drain tick, in addition to checking the drained task's own state, also check
`_collect_runnable()` (excluding that task) and dispatch anything else pending, before
re-checking. Turns the drain loop into a small, nested scheduling loop rather than a
passive wait.
*Gets:* `_scheduler_loop`'s own top-level loop and its `DeadlockError` condition stay
untouched; no new bookkeeping on `Task.state` at all, so no parent block/wake mechanics to
get wrong a third time.
*Costs:* a genuinely nested dispatch path needs its own determinism argument — does
dispatching a sibling from inside another task's drain loop still produce the same
`schedule_decision` sequence regardless of real timing? That is exactly the kind of claim
`test_pregel_reducer_write_order_is_deterministic.py` exists to check for `apply_writes`,
and this would need its own equivalent, not an assumption. Reentrancy also needs thinking
through: a dispatched sibling that itself fans out (nested subgraphs) nests the drain loop
again.

**Candidate β — give the deadlock check a grace period for backgrounded work.** Formalize
what §8.3's second attempt was reaching for: a task can be `BLOCKED`-and-known-to-be-
independently-active, and `_scheduler_loop`'s deadlock check grants such a task real ticks
(bounded, the same shape `resume_drain_ticks` already uses) before concluding deadlock,
rather than raising immediately — but, unlike that attempt, *never* re-dispatches the
parent through `_resume_task` at all; it simply lets `_drive_coro`'s own existing
completion path reach it in its own time.
*Gets:* closest in shape to what was actually tried, this time without the redundant-
dispatch bug.
*Costs:* touches `_scheduler_loop`'s deadlock condition directly — the highest-blast-radius
option here, on file this module's own docstring calls "the whole product" for I1. Needs
its own decisive experiment before anyone trusts it, the same discipline §3's own
measurement got.

**Candidate γ — revisit Option A in light of this, not on the original grounds.** §4
rejected Option A (the scheduler drives Pregel superstep-by-superstep) for reaching into
Pregel's non-public step API — that cost is unchanged and this finding does not revisit it.
What *is* new: both dispatch problems in §8.3 exist *because* Option B leaves Pregel in
charge of concurrency the scheduler itself never creates. Option A would not have either
bug, by construction — the scheduler would decide when each fanned-out branch proceeds
directly, never handing control to a hidden Pregel-created task at all. Recorded because
the *shape* of the problem this section found is evidence relevant to that original
trade-off, not because the trade-off's price (LangGraph internal-API coupling) has changed.

### 8.5 What this does not touch

D-78 and G10's second failure are exactly as §7 already states — unaffected by anything in
this section. ADR-018 (`begin_call`/`end_call`, §8.2) stands regardless of which candidate
above is eventually chosen or built: none of them require reverting it, since the identity
collision it closes is a real, separate bug from the dispatch gap, found first, fixed first,
and orthogonal to how the dispatch gap is eventually resolved.

### 8.6 Candidate α, attempted (2026-09-03) — a real cost §8.4 did not anticipate

The owner picked α to build. §8.4's own cost line for it named one risk — a determinism
argument for nested dispatch, not yet checked — and estimated the smallest blast radius of
the three: no new `Task.state` bookkeeping, `_scheduler_loop`'s own loop and its
`DeadlockError` condition both untouched. That estimate undercounted. Building it surfaced a
second, unrelated cost before the determinism question was ever reached, and this one is not
a "needs its own experiment" risk — it is an immediate, reproducible break in a **never-
waived gate's** own supporting module, confirmed empirically twice, in two different shapes.

**What was built.** `_resume_task`'s drain loop, on each tick, additionally collects other
runnable tasks (excluding the one being drained) and dispatches them via `_choose` —
preserving `_choose`'s own declared status as the module's single scheduling decision
point — before re-checking the drained task's own state. Full suite run immediately, before
anything else, per this session's own standing practice.

**First variant: nested dispatch increments `self._step` and consults `_delay_schedule`,
identically to a top-level turn.** Broke `tests/unit/explore/test_schedule.py`'s own decisive
test, `test_decision_step_matches_live_scheduler_at_every_branch_point`: **1 failed, 2135
passed** (full suite), confirmed causally attributable to this change alone by reverting it in
isolation (12/12 in that file pass with the revert, 11/12 with it). Root cause:
`explore/schedule.py`'s own `DelaySchedule` docstring proves, and its own decisive test
verifies against a live `Scheduler`, that `decision_step = sched_step - 1` holds *only*
because `self._step` increments exactly once per `_choose()` call, in strict 1:1 lockstep
with `_scheduler_loop`'s own top-level turns — nothing else has ever called `_choose` before
this change. Nested dispatch also calling `_choose`/incrementing `self._step` breaks that
lockstep: step numbers arrive in uneven bursts, so `decision_step = sched_step - 1` no longer
addresses the same logical top-level turn across a default run and a steered counterfactual
of it, because the two runs can trigger different amounts of nested dispatch before reaching
it. `explore/` is P13 (FR-6) — its bounded-exploration harness is what **G2 (never-waived,
§44.3)** depends on, per CONTEXT.md's own invariant table. This is a materially worse finding
than an I1 determinism question still needing an experiment: it is an already-confirmed break
in a never-waived gate's supporting module, no experiment required to see it.

**Second variant, tried after owner approval to redesign rather than abandon α: nested
dispatch does *not* touch `self._step` at all** (no increment, `_delay_schedule` skipped
entirely for nested picks — priority-pick only, no rng consumed). This does not fix the
problem, it relocates and worsens it. `docs/event-schema.md`'s own field-level contract for
`schedule_decision` marks `chosen_task_id` **stable** — part of the canonical projection,
required to be byte-identical across replays — and `explore/schedule.py`'s `turns_from_events`
enforces a hard, load-bearing invariant on top of that: two `schedule_decision` events may
never share a `sched_step` with different `chosen_task_id`s, raising `MalformedRunError` if
they do, because `sched_step` is stamped from `self._sched._step` on *every* event
(`_SchedulerRecorder.write`, not just `schedule_decision`), and is documented as "unique per
turn by construction." Not incrementing `self._step` for nested picks means the parent
task's own turn-start event and the first nested pick's turn-start event collide on the same
`sched_step` immediately. Confirmed empirically, same test file: **`MalformedRunError:
sched_step 3 has two schedule_decision events naming different chosen_task_id values — not
one coherent run`** — a hard crash, not a subtle mismatch, and now 2 of 12 tests in that file
fail rather than 1.

**Why this is not a numbering bug to patch.** Both directions were tried; both are wrong, for
structurally different reasons, which pins the problem down precisely: `explore/`'s entire
model — `Turn`, `turns_from_events`, `decision_step`'s arithmetic, and by extension
`generate.py`'s BFS, which writes `decision_step` values back into new `DelaySchedule`s to
explore — assumes exactly one `schedule_decision` event exists per distinct scheduling
decision, addressed by a `sched_step` sequence that increments once per decision with no
gaps and no two decisions sharing a value. Candidate α's whole premise — a second, nested
category of scheduling decision, made *during* another task's drain window rather than at
`_scheduler_loop`'s own top level — has no representation in that model at all. Neither
"share the parent's step" nor "take a fresh one from the same sequence" is a numbering choice
within that model's existing vocabulary; both violate an invariant the model states outright.

**What closing this gap for real would require, not attempted here.** Some way for a
nested-dispatch decision to be distinguishable from a top-level turn in the event stream
itself — concretely, something like a new field on `schedule_decision`'s payload (e.g. a
`nested: bool` or a parent-step reference), which is an **event schema change** — plus
matching updates to `explore/schedule.py`'s `Turn`/`turns_from_events`/`decision_step` to
either ignore nested decisions when computing top-level addressing or represent them as a
new, first-class kind of node in the schedule tree, plus a corresponding update to
`generate.py`'s BFS so its exploration still terminates and still explores what PRD §15
intends once "the next decision" is no longer a flat sequence. This is real, multi-module
design and implementation work — not something to fold into a nested-dispatch tweak inside
`scheduler.py`, and changing the event schema is item one on this whole engagement's own
standing stop conditions. Not scoped or attempted; recorded here as what α actually costs,
now that it is known rather than estimated.

**Status.** Both spike variants were reverted in full — confirmed via `git diff --stat`
showing zero diff on `scheduler.py`, and the full suite re-confirmed green (2136 passed, 13
deselected, matching the pre-spike baseline) immediately after each revert. Nothing from this
investigation is in the working tree. §8.4's cost/benefit weighing for α, β and γ should be
re-read with this section in mind — α's "smallest blast radius" framing no longer holds
uncontested; whether it still wins depends on how the owner weighs "touches three modules,
including a schema change, but avoids `_scheduler_loop`'s deadlock condition" against β's
"stays in one module and one function, but touches the deadlock condition itself" or γ's
"reopens an already-decided trade-off, but sidesteps this entire problem class by
construction." No candidate is chosen as of this writing.
