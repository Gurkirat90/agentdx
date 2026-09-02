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
