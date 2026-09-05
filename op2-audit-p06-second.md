# OP-2 INDEPENDENT AUDIT (SECOND PASS) — P06 `runtime/`

**Scope.** `src/agentdx/runtime/scheduler.py`, `runtime/clock.py`, `runtime/determinism.py`
(`runtime/faults/` and `runtime/cache/` excluded — separately audited elsewhere). This is
`runtime/`'s second-ever independent audit. The first (2026-08-14) found a PRD §14.2
vector-clock defect and an `sdk.generic.Recorder` mismatch, both repaired same day. Since then
`scheduler.py` went through a large, self-verified-only change sequence (ADR-017/018/019/
020/021/022) fixing a real dispatch deadlock under LangGraph fan-out — never independently
re-audited except for one narrow OP-2 of candidate β's `_backgrounded` bookkeeping (task #11,
which found and fixed one real staleness bug, ADR-022).

**Method.** Fresh read of `AGENTS.md`, the relevant PRD sections (§9 event schema, §10
deterministic runtime, §14.1-14.4 causality/vector-clock, §8.8 context/identity), `CONTEXT.md`
§8's ADR-017 through ADR-022 rows, and the full text of `scheduler.py` (1701 lines) end to end.
No source file was edited. Every claim below was executed, not inferred: seven throwaway repro
scripts under `/tmp/audit_repro/` against the real `Scheduler` class (not a mock), three real
`agentdx run` CLI invocations per reference fixture via the sandbox's working venv
(`/tmp/optb_venv`, `PYTHONHASHSEED=0`), a canonical-hash diff of the resulting SQLite event logs,
a 180-run randomized fuzz harness, and a direct empirical collision search against
`make_run_id`. The existing `runtime/` test suite (`tests/unit/runtime/`,
`tests/integration/runtime/`) was run as a baseline: 100% pass, no changes needed to reach that
baseline.

---

## VERDICT: **FAIL**

**The dispatch/concurrency fix (ADR-017/018/019/022) holds up well for the one shape it was
built and tested against** — LangGraph Pregel-style fan-out via `run_node_async`
(`begin_call`/`spawn`/`join`/`end_call`). N-way (tested to 8-way) fan-out, nested/recursive
fan-out, and an exception raised inside a fan-out branch all complete cleanly with no leaked
`_backgrounded`/`_identity_owners`/`_call_tokens` state. Determinism (I1) holds: three fresh
`code_pipeline` runs, two `research_fanout` runs and two `support_triage` runs, all via the real
CLI on this sandbox, produced byte-identical canonical projections (only the documented volatile
`run_start.payload.pid` field differed).

**But the fix's scope is narrower than the concurrency contract this project actually promises**,
and two of the three checks the audit brief specifically asked about (item 1 — the dispatch fix's
generality; item 3 — whether the root-entry fix generalizes; item 4 — `run_id` collision safety)
each found a real, demonstrated defect:

1. **(CRITICAL)** `yield_point()`/`sleep()` have no analog of `begin_call`'s identity protection.
   Any two concurrent SDK calls that share one ambient scheduler identity — which is exactly what
   happens for two concurrent LLM calls made via `asyncio.gather` within one agent step, a pattern
   PRD §8.8 explicitly says is supported — silently collide, orphaning one call's `Future`
   forever and eventually raising `SchedulerError` (or `DeadlockError`) with a misleading message.
2. **(HIGH)** ADR-021's root-entry fix (`yield_point("sdk_run_entry")`) is wired into
   `sdk/generic.py::run()` only. It does not generalize to a *spawned* node body's own first
   dispatch; a node whose own body immediately fans out (e.g. a subgraph node) reproduces the
   identical `DeadlockError` ADR-021 fixed for root, one level down.
3. **(MEDIUM-HIGH)** `make_run_id`'s 32-bit run-id hash has a real, demonstrated (not
   theoretical) collision rate, and ADR-020's new "sealed collision ⇒ silently reuse" logic never
   cross-checks the stored run's actual `(seed, scenario_hash, graph_hash)` before reusing its
   verdict — a genuine collision between two *different* scenarios silently reports the wrong,
   stale result.

None of these three is restated from a prior audit or a documented limitation; all three are new,
demonstrated below.

---

## FINDING #1 (CRITICAL) — `yield_point`/`sleep` share `join`'s pre-ADR-018 ambient-identity collision, and were never given `begin_call`'s protection

**Where:** `src/agentdx/runtime/scheduler.py:660-701` (`yield_point`) and `:702-733` (`sleep`).
Both resolve the caller via `_current_task_id()` (ambient `contextvars` lookup,
`runtime/context.py`) and then do `self._task_futures[task_id] = fut` unconditionally — with
**no check** analogous to `begin_call`'s `self._identity_owners.get(current_ctx.task_id) is
current_real_task` (line 1052-1056). Real call sites that invoke `yield_point` directly, with
**no `begin_call` wrapping at all**: `src/agentdx/sdk/providers/openai_compatible.py:299,308`
(`await run.scheduler.yield_point("llm_call")` / `"llm_call_done"`) — i.e. **every real LLM
call**. `scheduler.sleep()`/`agentdx.sleep()` are equally exposed for any concurrent
virtual-time wait.

**Why this is reachable, not contrived.** PRD §8.8 states, as a guarantee: *"Concurrent
sub-tasks within one agent receive derived clock slots (`coder#1`, `coder#2`)... `RunContext`
and `AgentContext`... live in `contextvars`, so `asyncio` tasks inherit correctly and no explicit
threading of parameters is required."* This is the PRD's own description of exactly the pattern
that breaks: a node body doing `await asyncio.gather(call_llm(...), call_llm(...))` — two
ordinary concurrent LLM calls, the textbook "agent racing itself" scenario §14.2 exists to
detect. `agent_scope`/`_enter_agent` (`sdk/generic.py:1349-1386`) correctly derives a distinct
*clock slot* for concurrent scopes of the same agent (the event-log/vclock side of this
contract), but that is a completely separate `contextvars` slot (`_AGENT`) from the one
`yield_point`/`sleep`/`join` key off (`runtime/context.py`'s `SchedTaskContext`/`active_task`) —
fixing the vclock side did nothing for the scheduler-dispatch side.

`begin_call`/`end_call` (ADR-018) is the *only* place this ambient-identity collision was fixed,
and it is wired into exactly one call site: `sdk/langgraph.py::run_node_async` (lines 694, 704).
It protects Pregel's own node-level fan-out. It does nothing for concurrent calls made *within*
one node/agent's own body via raw `asyncio.gather`/`ensure_future` — which is the only way to
make two concurrent LLM or tool calls today, since neither the LLM provider shim nor `agent_scope`
calls `begin_call`.

**Demonstrated, real `Scheduler`, two ways:**

```
$ python3 /tmp/audit_repro/repro6b_concurrent_yieldpoint_collision.py
OTHER EXCEPTION: SchedulerError: [E-SCHED-001] task 't_r_00006_root_0', resumed from its own
join/yield/sleep Future, did not reach a scheduler-recognised state (DONE, or a fresh
suspension) within 200 real event-loop ticks — its continuation appears to be genuinely stuck
on real, unmanaged concurrency rather than merely unwinding through non-scheduler asyncio
machinery (docs/determinism-guarantees.md#e-sched-001)
order so far: ['a-before', 'b-before', 'b-after']
```

The harness: root does one sequential warm-up call (mirroring `planner`, so this is a genuine
*resumption* with the full `resume_drain_ticks` grace, not the already-known first-dispatch
limitation), then does `await asyncio.gather(_llm_call("a"), _llm_call("b"))` where `_llm_call`
calls `scheduler.yield_point(...)` directly — the exact shape of two concurrent LLM calls. The
trace (`'a-before', 'b-before', 'b-after'` — **`'a-after'` never appears**) shows `_llm_call("a")`
is permanently orphaned: its `Future` was silently overwritten in `self._task_futures[root_id]`
by `_llm_call("b")`'s own `yield_point` call before "a" ever suspended. `_resume_task` then keeps
resolving "b"'s future forever (root's `Task.state` never leaves `RUNNING` because "b" finishing
touches no `Task` object), until the 200-tick drain budget is exhausted and it raises. The
identical mechanism reproduces for `sleep()`:

```
$ python3 /tmp/audit_repro/repro7_concurrent_sleep_collision.py
EXCEPTION: SchedulerError: [E-SCHED-001] ...
order so far: ['x-before', 'y-before', 'y-after']
```

(Two concurrent `scheduler.sleep()` calls — e.g. two independent retry backoffs within one agent
step — collide the same way; `'x-after'` never appears.)

**Confirmed not already covered by any existing test.** `tests/unit/runtime/test_scheduler.py`'s
own "`test_concurrent_begin_call_mints_distinct_non_colliding_identities`" (line 648) tests
*exactly* this shape (`asyncio.gather` over two children racing on root's ambient identity) — but
each child explicitly calls `begin_call` first (line 664), which is precisely the protection this
finding shows is missing from the two call sites (`yield_point`, `sleep`) that real LLM/tool code
actually uses. No test in the repository combines the real `Scheduler` (not the no-op
`ImmediateScheduler`, `sdk/generic.py:445-511`, whose `yield_point` is `return` — a genuine no-op
with no ambient identity to collide on) with concurrent `yield_point`/`sleep` calls made outside
a `begin_call` bracket (`grep -rn asyncio.gather tests/` combined with LLM/tool call sites: zero
matches). None of the three reference fixtures makes a raw concurrent `asyncio.gather`/
`ensure_future` call inside a node body either (`grep -rn "asyncio.gather\|ensure_future"
fixtures/*/graph.py`: zero matches) — all their concurrency is LangGraph-node-level, which is
exactly the one path `begin_call` protects. This is why the defect has never surfaced in any
fixture, acceptance run, or CI gate.

**Severity.** Critical. This directly contradicts a PRD §8.8 guarantee, affects the single most
common form of real agent concurrency (concurrent LLM/tool calls within one step, not just
LangGraph-level fan-out), fails with a misleading message ("genuinely stuck on real, unmanaged
concurrency" — it is not; it is a scheduler bookkeeping bug), and can also manifest as a silent
*hang* rather than an exception if the "losing" continuation's orphaned coroutine happens to be
awaited by something that never times out.

**Suggested fix direction (not mandatory).** Give `yield_point`/`sleep` the same
real-task-ownership check `begin_call` already has: at entry, compare
`asyncio.current_task()` against `self._identity_owners.get(task_id)`; if they differ, the
caller is a different real task that merely inherited a copy of the ambient identity, and needs
`begin_call`'s mint-a-fresh-identity treatment applied inline rather than proceeding to silently
overwrite `self._task_futures[task_id]`. At minimum, detect the overwrite itself (a live,
unresolved `Future` already present at `self._task_futures[task_id]` when a new one is about to
be installed) and raise a clear, immediate, correctly-diagnosed error rather than waiting 200
ticks to report a misleading one.

---

## FINDING #2 (HIGH) — the root-entry fix (ADR-021) does not generalize to a spawned task's own first dispatch

**Where:** `src/agentdx/sdk/generic.py:1879` (`await context.scheduler.yield_point
("sdk_run_entry")`, inside `run()`, called once, unconditionally, before `_invoke(graph,
payload)`). This is the *only* call site that constructs a root coroutine and calls
`scheduler.run(...)` (confirmed: `grep -rn "scheduler.run("` finds exactly one call site,
`cli/commands/run.py:337`, always wrapping `agentdx.run(...)`), so root itself is universally
protected in every real invocation. **Nothing equivalent exists in
`src/agentdx/sdk/langgraph.py::_run_node_body`** (lines 706-739), which is what actually drives
every node's own `base.ainvoke(...)` call after `run_node_async` (`langgraph.py:642-704`)
`spawn()`s it as its own scheduler task.

**Why this matters.** `_resume_task` (`scheduler.py:1301-1403`) grants exactly one real tick to a
task's *first* dispatch (the `else` branch, line 1396-1403) and up to `resume_drain_ticks` (200)
only to a *resumption* (the `fut is not None` branch, line 1370-1395). ADR-021 exists because
root's own first dispatch, with no prior scheduler-visible checkpoint, could not survive
LangGraph's own multi-tick entry ceremony — its fix was to force root through one guaranteed
`yield_point` before `_invoke` is ever reached, so by the time root touches Pregel machinery it is
on a *resumption*, not its raw first dispatch. This reasoning applies identically to **any**
spawned node body whose own first action — before it has made any scheduler-visible call of its
own — invokes something that needs more than one real tick to settle. A node whose body itself
invokes a subgraph, and whose subgraph's first superstep already has two or more ready nodes (a
perfectly ordinary graph shape: `START -> {a, b}`), reproduces exactly this failure, one level
down, in code no prior audit or test exercised.

**Demonstrated, real `Scheduler`:**

```
$ python3 /tmp/audit_repro/repro5_nested_immediate_fanout.py
DEADLOCK (confirms the hypothesis): [E-SCHED-003] deadlock: no task is runnable and none is
blocked on a timer. Wait reasons: {t_r_00006_coder_0: ; t_r_00006_root_0:
join(t_r_00006_coder_0)} (docs/determinism-guarantees.md#e-sched-003)
```

Root itself is warmed up correctly (a sequential `planner`-style call first, mirroring
ADR-021's own effect) — root is not what deadlocks. `coder`'s own spawned body (`t_..._coder_0`,
empty wait-reason — the signature of a task stuck on its own untouched first dispatch) is what
deadlocks, because *its* first action is an immediate two-way concurrent fan-out
(`begin_call`+`spawn`+`join`, twice, via `asyncio.ensure_future`), with no sequential
warm-up inside `coder`'s own body first.

This same shape also explains 13/55 failures in a 180-run fuzz sweep (`/tmp/audit_repro/
repro4_fuzz.py`, randomized branching fan-out trees, seeds 0-59 × scheduler seeds {1,7,42}):
9-plus of them show a `join(...)` wait-reason on the *parent* (proving the parent itself warmed
up correctly) paired with an *empty* wait-reason on the child it's joining — the exact signature
above, at an arbitrary nesting depth, not just the hand-built two-level case.

**Severity.** High, not critical, because whether this is reachable through the *shipped*
LangGraph adapter today depends on whether subgraphs are a supported/instrumented construct
there (not verified this pass — worth checking directly); but the failure is real against the
`Scheduler` class itself regardless of that, `run_node_async` is the only real call site and does
nothing to prevent it, and the general principle ("a task's first dispatch survives only one
real tick, and only root gets a guaranteed warm-up") is not documented anywhere as a known
per-node limitation.

**Suggested fix direction (not mandatory).** Generalize ADR-021: either have `_drive_coro`
(`scheduler.py:1405-1454`) issue one internal, non-`schedule_decision`-emitting warm-up
equivalent to every task before its first real line of user code runs (symmetric with how root
is now treated), or add the same one `yield_point` call ADR-021 added to root, to the top of
`_run_node_body` (`langgraph.py:706`) before `base.ainvoke(...)` is reached.

---

## FINDING #3 (MEDIUM-HIGH) — `make_run_id`'s 32-bit id plus ADR-020's silent-reuse path can misattribute a stale verdict to a different scenario

**Where:** `src/agentdx/runtime/scheduler.py:1665-1686` (`make_run_id`), specifically
`blake2b(material, digest_size=4)` — a **32-bit** id space. Consumed by
`src/agentdx/cli/host.py:253-256` (`CliRunHost.open_run`):

```python
existing = self._store.get_run(self._run_id)
if existing is not None:
    if existing.sealed:
        raise RunAlreadyExistsError(existing)
```

and `src/agentdx/cli/commands/run.py:853-876`, which catches `RunAlreadyExistsError` and reuses
`exc.record.run_id`'s stored events/verdict directly. **Neither site compares
`existing.scenario_hash`/`existing.graph_hash`/the resolved `seed` against the current
invocation's values before treating the match as "identical run."** `RunRecord` (`cli/host.py`)
carries exactly those fields — the data needed to detect a genuine collision is available at
both sites and is simply not checked.

**This is a real, not theoretical, collision rate.** A 32-bit space means a ~50% chance of at
least one collision after roughly 77,000 distinct `(seed, scenario_hash, graph_hash)` triples
(birthday bound), a scale a CI reliability gate (FR-11b, the product's own stated use case)
running many scenarios × many seeds accumulates over weeks, not years, for a store that keeps
growing (`.agentdx-data/`, no eviction observed).

**Demonstrated, real `make_run_id`, real collision found in under 30,000 tries, 0.1s:**

```
$ python3 -c "
from agentdx.runtime.scheduler import make_run_id
seen = {}
i = 0
while True:
    i += 1
    seed, sh, gh = i, f'blake2b:scenario-{i}', f'blake2b:graph-{i}'
    rid = make_run_id(seed, sh, gh)
    if rid in seen and seen[rid] != (seed, sh, gh):
        print(rid, seen[rid], (seed, sh, gh)); break
    seen[rid] = (seed, sh, gh)
"
r_1c03595a (19884, 'blake2b:scenario-19884', 'blake2b:graph-19884') (27368, 'blake2b:scenario-27368', 'blake2b:graph-27368')
```

Two **completely different** inputs (different seed, different scenario, different graph)
produce the identical `run_id`. Under the current `open_run`/`_run_and_score` logic, if input A
ran first and sealed, a later invocation of input B would hit `RunAlreadyExistsError`, and the
CLI would silently print input A's stored verdict, exit code and findings as if they were the
result of running input B — a completely different scenario. This is a real I9 ("no fabricated
results") risk: the printed result is fabricated with respect to what was actually asked to run,
and nothing in the output signals it (the `reused: true` marker introduced by ADR-020, per
`CONTEXT.md`'s own row, is designed to *distinguish a legitimate identical rerun*, not to catch a
collision between different inputs — it fires identically in both cases).

**This is a regression, not a pre-existing risk with unchanged consequences.** `digest_size=4`
predates this session (present since the original P06 commit, confirmed via `git log -p`), but
prior to ADR-020 a `run_id` collision on `create_run` raised `E-STORE-010` as a hard failure —
safe, if unhelpful. ADR-020 changed the consequence of the same underlying weak hash from "hard
error" to "silently succeed with someone else's answer."

**Severity.** Medium-High: the collision precondition (any run_id collision at all) is rare per
run but the failure mode when it does occur is silent and high-consequence (a wrong pass/fail
verdict, wrong exit code, for a CI gate), and the fix is cheap.

**Suggested fix direction (not mandatory).** Independent of widening the hash (which only lowers
the odds, not the risk class): at both `open_run` (line 253-256) and the `RunAlreadyExistsError`
handler (`run.py:859-863`), compare `existing.scenario_hash == self._scenario_hash`,
`existing.graph_hash == self._graph_hash`, and `existing.seed == resolved_seed` before treating
the match as a legitimate rerun; on a mismatch, raise a new, distinct error
(e.g. `E-STORE-011`, "run_id collision between different inputs — extremely unlikely but
detected; treat as a bug report") rather than silently reusing. Widening `digest_size` (e.g. to
8 or 16 bytes) is a good complementary defense-in-depth change but does not by itself close the
silent-misattribution risk class.

---

## What was checked and holds up (no defect found)

- **N-way concurrent fan-out from one parent** (`begin_call`/`spawn`/`join`, the protected path):
  tested 2, 3, 4, 5 and 8-way, each followed by a sequential stage after the fan-out (mirroring
  `tester`). All complete cleanly; `_backgrounded`/`_identity_owners`/`_call_tokens` all empty
  after the run; every task reaches `DONE`. (`/tmp/audit_repro/repro1_threeway_fanout.py`)
- **Nested/recursive fan-out**: a fanned-out node body itself performing its own further
  concurrent fan-out (mirroring a subgraph node whose body fans out again), *after* a sequential
  warm-up so the first-dispatch limitation (Finding #2) doesn't mask the question — completes
  cleanly, no leaks. Traced through the mechanism by hand: `_drive_coro`'s completion `finally`
  (line 1434-1440) is a correct fallback for a *spawned* task's own nested `_backgrounded` entry
  (unlike a `begin_call`-minted identity, a spawned task is always eventually driven to `DONE` by
  `_drive_coro`), and `_clear_backgrounded_checkin` correctly fires for the nested parent's own
  next check-in regardless of nesting depth, since each nesting level rebinds ambient identity to
  its own genuinely-driven `Task`. (`/tmp/audit_repro/repro2_nested_fanout.py`)
- **Exception raised inside one of several concurrent fan-out branches**: propagates correctly
  through `join()`, `end_call` cleanup still runs (the `finally` contract holds), no leaked
  bookkeeping; siblings that were still in flight when the exception surfaced continued running
  to completion rather than being abandoned mid-state (`asyncio.gather`'s own default semantics —
  not a scheduler bug, but worth noting: `_scheduler_loop` keeps driving every task
  (`_has_remaining_tasks`, line 1257-1259, checks *all* tasks) to `DONE` even after the "failing"
  parent's own coroutine has already unwound, which is inconsistent with this file's own module
  docstring claiming "loop ends when root task completes" (line 809) — cosmetic, not a
  correctness defect, but the docstring should say "when every task reaches DONE").
  (`/tmp/audit_repro/repro3_exception_fanout.py`)
- **Determinism (I1) under the new dispatch machinery**: three fresh `code_pipeline` runs (seed
  42), two `research_fanout` runs (seed 7), two `support_triage` runs (seed 7), each via the real
  `agentdx` CLI on this sandbox (`PYTHONHASHSEED=0`), each into a fresh `--data-dir`. Canonical
  projections (§10.7: strip `wall_ts_ms`, `payload.duration_wall_ms`, `payload.cache_key`, and
  `run_start.payload.{host,pid,started_at_utc,env}`, then `blake2b` over sorted-key JSON per
  event) are **byte-identical within each fixture** across all repeats. The only field that ever
  differed pre-canonicalisation was `run_start.payload.pid` — exactly the documented volatile
  field. No dependency on wall-clock timing, real asyncio scheduling order, or dict/set iteration
  was found leaking into the canonical form for any of the three fixtures.
- **`_backgrounded` staleness fix (ADR-022)**: held up under every probe run this pass, including
  shapes ADR-022's own two tests don't cover (3+ way and nested fan-out) — check-in-based
  clearing generalizes correctly because it is keyed to "the next genuine scheduler call from
  this exact ambient identity," which is well-defined regardless of fan-out width or nesting
  depth.
- **The 180-run fuzz sweep's 55 failures** (`/tmp/audit_repro/repro4_fuzz.py`, randomized
  branching fan-out/sleep trees, no `begin_call`-bypass shapes — only via the protected
  `begin_call`/`spawn`/`join` path): every failure traced to either the already-documented
  root/no-prior-step limitation or Finding #2's generalization of it (a spawned task's own first
  dispatch). No third, distinct failure signature was found in this sweep.

---

## NOT DONE / RISKS

- Whether Finding #2 is reachable through the *shipped* LangGraph adapter specifically (i.e.
  whether compiled subgraphs are actually instrumented/supported by `sdk/langgraph.py` today) was
  not verified — the finding is proven against the real `Scheduler`+the real `begin_call`/`spawn`/
  `join` call shape `run_node_async` uses, but whether LangGraph subgraphs specifically reach that
  shape in this codebase's current adapter is a follow-up check for the repair session.
  Independent of that question, Finding #1 (concurrent LLM calls) is reachable through the shipped
  provider shim with certainty — it needs no subgraph at all.
- Did not fuzz `clock.py`/`determinism.py` independently beyond what the existing suite and the
  determinism cross-run check above cover; no defect was found there, but the effort budget went
  to the dispatch/concurrency fix per the brief's own stated priority.
- Did not re-verify on real Python 3.12 hardware — this sandbox is Python 3.10 (the same standing
  gap every recent ADR row in `CONTEXT.md` notes, D-66). All three findings are demonstrated
  against the real `Scheduler` class either way; nothing about them is Python-version-specific
  in mechanism (pure `contextvars`/`asyncio.Future` bookkeeping), but real-hardware confirmation
  is still owed.
- Did not attempt to find a real fixture-driven realization of Finding #3 (an actual
  `(seed, scenario_hash, graph_hash)` collision between two of the three reference fixtures) —
  the empirical collision search targets `make_run_id` directly, which is a faithful and much
  faster proof of the same underlying weakness without needing to brute-force real fixture
  content hashes.
- Repro scripts live at `/tmp/audit_repro/repro1..repro7*.py` (this sandbox) — disposable,
  not part of the repository; re-run any of them against a repaired scheduler to confirm a fix.
