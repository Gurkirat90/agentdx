# D-62 design — making an agent step a scheduler task

**Status:** design only. No code. Written to be reviewed and argued with before anyone
implements it, because the cheapest thing to get wrong here is the framing, and the framing
in `CONTEXT.md` today is imprecise.

Everything below is from reading the shipped code. Line references are real. Section 3 is a
**hypothesis with an experiment attached** — run the experiment before building anything.

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

So a task that suspends on anything **else** — any real async machinery needing more than one
tick — stays in `RUNNING`. It is not runnable, it has no timer, and `_has_remaining_tasks()`
is still true. `_scheduler_loop` raises `DeadlockError` (`E-SCHED-003`).

That is exactly the observed failure: `{t_r_50f3b6_root_0: }` — one root task, **empty**
`wait_reason`, because `wait_reason` is only set by `yield_point`/`sleep` and the root never
reached either.

## 3. The framing correction — and the experiment that settles it

`CONTEXT.md` §7/§9, the `Dockerfile` header, and this session's own earlier notes all say the
deadlock is caused by **LangGraph's parallel fan-out**. Reading the loop, that looks wrong, or
at least unproven.

**Hypothesis: parallelism is incidental. The scheduler requires every in-task suspension to
be a scheduler Future and grants one event-loop tick between resumptions. Any `await` on real
async machinery that needs more than one tick deadlocks it — fan-out or not.** A strictly
sequential LangGraph graph should deadlock the same way, provided its `ainvoke` awaits
anything that does not settle in a single tick.

**The experiment, before any design is chosen:** build a single-node, strictly sequential
LangGraph graph with no fan-out and no LLM call, instrument it, and run it under a real
`Scheduler`.

- **Deadlocks** → the hypothesis holds. The problem is the task/suspension contract, and
  fan-out is a red herring. Options A and B below are both about ownership of *suspension*,
  not of *parallelism*.
- **Completes** → the hypothesis is wrong, fan-out really is the trigger, and the design space
  narrows to concurrency only.

This is one small test and it changes which design is correct. Do not skip it. Note that the
ledger has already misattributed one failure in this exact area (G9's exit 7 was blamed on
D-62 for two days, and was actually target resolution — D-77).

## 4. Three designs

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

1. **Run the §3 experiment.** One test, and it decides the framing.
2. **ADR for the Protocol change.** `sdk.generic.Scheduler` gaining `spawn` is a public
   interface change (`AGENTS.md` §3). Write it before the code.
3. **Pick A or B on the experiment's evidence**, not on this document's guess. If the
   hypothesis holds, B is the smaller bet and A is the one that closes D-55.
4. **Re-run G3 and G2 first**, before G1 or G4. If real execution breaks determinism or
   produces a false positive on the healthy fixture, that is the finding — and both gates are
   on PRD §44.3's never-waived list.
5. Only then G1, G4, and the demo gates.

## 7. What this does not touch

D-78 (`run_id` collision) is independent and blocks G9 on its own — see `d78-plan.md`.
G10 has a **second** independent failure beyond D-62: the one cold measurement, 181.092 s,
exceeds the 180 s threshold. Closing D-62 does not close G10.
