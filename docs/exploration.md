# Bounded schedule exploration (`agentdx.explore`)

PRD §15 (bounded schedule exploration), FR-6, invariants I1/I4/I10, gate G2's "harder half."
Status: **BUILT — self-reported, no OP-2 audit yet** (see `CONTEXT.md` §5/§13 for the ledger
entry). This document states plainly what is proven, against what, and what is not — the
mission's own instruction, restated here for a reader who did not see the mission brief.

## The one rule everything else follows from

**Bounded search is not exhaustive search, and nothing in this module's output may be read as
if it were.** `explore()` runs a finite number of interleavings out of a combinatorially larger
space; every report it produces carries the PRD §15.6 coverage statement, verbatim, as a
required field — not a caption a UI can omit, not text a caller can paraphrase. Invariant I10:
*"Bounded search: absence of findings is not proof of absence."* This is a deliverable of equal
weight to the search itself (mission Design Constraint 1) — `report.py`'s module docstring
says so, and `report.Report.__post_init__` enforces it at the type level: a `Report` cannot be
constructed with any other coverage text.

## What this module does

`explore()` (`explore/generate.py`) runs PRD §15.3's breadth-first search over **delay
schedules** — `{decision_step: chosen_index}` maps that tell `runtime.scheduler.Scheduler`
which alternative to pick at a specific scheduling decision, instead of its normal seeded
choice. Starting from the empty (default) schedule, it explores by delay count — fewest
deviations first, so the simplest reproducing schedule is always found before a more complex
one — up to three independent termination bounds (PRD §15.5), all three checked on every
iteration, never just the union any one of them implies:

- **`k` (`delay_bound_k`)** — no schedule deviates at more than `k` decision points. Bounds
  the search tree's depth directly; a child is only generated when `len(schedule) < k`.
- **`N` (`schedule_cap_n`)** — no more than `N` schedules are ever executed.
- **`time_budget_s`** — a wall-clock ceiling, read once at the start of the search
  (`generate.Budget`, using the sanctioned `agentdx.wall_time()` accessor).

Defaults are `k=2`, `N=200`, `time_budget_s=120.0` (PRD §15.1's table), configurable through
`agentdx.toml`'s `[explore]` section, an environment variable, or a per-call argument — never
hardcoded in `generate.py`/`reduce.py` themselves (Design Constraint 2; `config.py`'s
`ExploreConfig`, following the D-12/D-20/D-38/D-43/D-50 precedent of a later prompt extending
`config.py` even though it is not in that prompt's own `DELIVERABLES`).

At each executed schedule's own turns, **independence-based reduction** (`reduce.py`, PRD
§15.4) decides which branching points are worth exploring at all, and **duplicate elimination**
(`dedup.py`, PRD §15.5, `schedule.signature()`) guarantees a schedule is never executed twice,
by content — not by object identity, not by dict insertion order (I1). `report.py` turns the
raw result into PRD §15.6's `Report`: the executed/unique/reduced/duplicate counts, every new
finding and the schedule it was first seen on, and the coverage statement.

## Why finding-detection is unchanged: no new detector

`report.build_report` calls `analysis.race.detect_conflicts` — the same P12 detector every
other caller uses — once per executed schedule, recording each new finding the first time its
`finding_id` appears. `explore/` adds no detector of its own (OUT OF SCOPE, honored literally):
its contribution is running that detector honestly across many interleavings and reporting the
result with the coverage caveat attached, not inventing a new way to find races.

## Independence-based reduction, and its honest limitation (PRD §15.4, Design Constraint 3)

v1 ships **independence-based reduction only**, not full DPOR or sleep sets. `agentdx.toml`'s
`[explore] upgrade_reduction_if_redundancy_over` (default `0.40`, Q-43.2.4) is the threshold at
which an upgrade would be *considered* — nothing in this build reads that value to switch
algorithms; it exists so a measured redundancy fraction is compared against a versioned,
printable config value rather than a bare literal repeated in prose. This build measures and
reports; it does not upgrade on instinct.

A scheduling point is *interesting* — worth branching on — unless proven not to matter:

1. Only one task was runnable (`Turn.choices_at == 1`): no choice exists.
2. Every runnable candidate's next operation is independent of the chosen task's own
   (`reduce.independent`: disjoint state keys, different message edges, different locks,
   different tool calls with different arguments).
3. A candidate with no observable operation at all is independent of everything, vacuously —
   this falls out of guard 2's `all()` over an empty sequence, not a special case.

**The empirical gap, stated once here rather than scattered as comments** — this is the
soundness caveat PRD §15.4 itself requires be stated, not hidden. This codebase's only
scheduler-visible yield point around a state/message/lock/tool operation is the LLM-call one;
those operations are not individually preemptible, so a reconstructed `Turn` may bundle several
of them. Guard 2 needs a *candidate that was not chosen*'s operation set to compare against —
information no static analysis in this build currently exposes (no `sdk/`/`runtime/` change is
in P13's `DELIVERABLES`). `reduce.py` resolves this honestly rather than guessing: a candidate's
operation set is read from **the most recent other turn in the same executed run where that
same task id was the one chosen** — a real, observed operation set, never invented. If a
candidate has never taken a turn anywhere earlier in the run, guard 2 **fails open**: the point
stays interesting. Soundness beats speed, always — an unproven "independent" is never assumed,
only a proven one is used to reduce. `tests/unit/explore/test_reduce.py::
test_interesting_steps_only_looks_strictly_earlier_never_future` is the regression test that
keeps this "strictly earlier, never a later turn in the same run" property from silently
regressing.

**Consequence, stated plainly:** on a graph where every task takes exactly one turn ever (a
single-shot fan-out — `research_fanout`'s own shape), guard 2 has nowhere "earlier" to look for
the *first* candidate it ever meets and never fires for it. Measured reduction on that shape is
genuinely uneven — some points reduce once every task has taken at least one turn, some never
do. That is a true number this build reports, not a defect in the guard.

## A second bug caught and fixed during this build

**The `decision_step` off-by-one.** `Scheduler._choose()` reads `self._step` *before* it is
incremented; `_scheduler_loop` only increments `self._step` afterward, and the
`schedule_decision` event for that turn is stamped with the incremented value. So the turn
recorded with `sched_step=N` was actually decided by the `_choose()` call made when
`self._step == N - 1`. Using the recorded `sched_step` directly as a `DelaySchedule` key — the
naive, PRD-pseudocode-literal reading — silently steers the *next* turn instead of the intended
one. `schedule.Turn.decision_step` (`= sched_step - 1`) is the one sanctioned conversion;
`generate.py`'s child-schedule construction uses it exclusively, and
`tests/unit/explore/test_schedule.py::test_decision_step_matches_live_scheduler` proves the
mapping against a real `Scheduler`, not just from reading the source — the same class of gap
`docs/race-detection.md` documents for `causality.build_causality`'s first build: a bug an
against-the-real-thing test catches and a hand-authored-only test suite would not.

**A `budget_exceeded`/`capped` conflation.** An earlier version of `generate.Budget` folded the
schedule-cap check and the wall-clock check into one `exceeded()` boolean, which `explore()`
then used directly for `ExplorationResult.budget_exceeded`. Reaching the cap alone therefore set
`budget_exceeded=True` too, which would have made `report.format_report` print "the time budget
was exhausted" on a run that never came close to its deadline — a direct violation of Design
Constraint 5's "be explicit about *which* bound stopped the search." Fixed by splitting the two
signals at the source: `Budget` now answers only "has the wall clock run out?", `explore()`
checks the cap and the clock as two separate conditions (cap first, so a tie is reported as
`capped`, never `budget_exceeded` — a stated, deterministic priority), and `capped` itself is
`True` only when the frontier still had unexplored work left (`bool(frontier)`), not merely
"executed exactly N schedules," so a run that naturally completes at exactly N is correctly
reported as complete, not truncated. `tests/unit/explore/test_generate.py`'s
`test_explore_cap_reached_never_sets_budget_exceeded` and
`test_explore_natural_completion_at_exactly_n_is_not_capped` are the regression tests.

## Why the demonstration below is a synthetic harness, not `code_pipeline`/`research_fanout` themselves

`explore()`'s `ScheduleExecutor` drives one run by passing a `delay_schedule` straight to a real
`runtime.scheduler.Scheduler`. The two reference fixtures execute through `sdk/langgraph.py`'s
`LangGraphAdapter`, which records LangGraph node reads/writes/spans but never routes LangGraph's
own Pregel dispatch through `Scheduler.yield_point`/`spawn` — so a `delay_schedule` has nothing
to act on for either fixture today. Wiring that is an `sdk/langgraph.py` change, out of P13's
`DELIVERABLES`. This gap was surfaced to, and the workaround explicitly approved by, the project
owner during this build: prove `explore/` for real against a Scheduler-driven harness *shaped
like* the two fixtures, rather than block P13 on the wiring gap. Nothing below pretends the
literal fixtures were explored — `tests/integration/explore/_harness.py`'s own module docstring
states this same limitation, so a reader of the code and a reader of this document see the same
claim.

**Two hand-built scenarios**, same posture as `tests/determinism/_harness.py` and
`tests/integration/faults/_harness.py` (fake tasks, no LLM, no graph, no fixture — mission
Design Constraint 6's own precedent):

- **`research_fanout`-shaped**: three worker tasks, each a couple of yield points then one
  `state_write` to a single shared key through a **declared reducer** (`reducer="merge"`) — PRD
  §14.7 guard G3's own worked example. No causal edge is ever declared between the three
  writers, so every pair of writes is concurrent (guard G1) by construction — the scenario
  exists to prove G2 holds *because* of the declared reducer, not because the writes happen to
  serialise.
- **`code_pipeline`-shaped**: two worker tasks, each a couple of yield points then one
  `state_write` to a shared key with **no** reducer and **no** lock — an unsynchronised
  pipeline race, producing a genuine `write_write` finding.

**Why finding-presence is schedule-invariant for both harnesses, stated honestly.**
`analysis.race.detect_conflicts` decides concurrency from the causality graph (a vector-clock
comparison), not from which physical interleaving the scheduler actually chose. Neither harness
above ever declares a `causes=` edge between one writer's write and another's, so every writer's
writes stay concurrent with every other's, in every vclock, regardless of `delay_schedule` — the
same finding (or absence of one) is present at every schedule in the frontier. Bounded
exploration's contribution here is not "finds a race no single run would" — it is running the
same P12 detector, honestly, across every executed interleaving and reporting that consistently,
rather than assuming one run generalises. A real production scenario where scheduling order
*does* change whether a causal edge gets established dynamically (for example, which of several
concurrent lock-acquire attempts wins, changing which writes end up ordered) would show
schedule-dependent findings; this build's synthetic harnesses were kept deliberately simple so
their expected output is hand-verifiable, at the cost of that particular realism.

## Definition of done, demonstrated

All four items below were run against the real `runtime.scheduler.Scheduler`
(`tests/integration/explore/test_dod.py`), not asserted from reading the source.

**1. `k=2`/`N=200` completes on the `code_pipeline`-shaped scenario, coverage statement
verbatim:**

```
Bounded schedule exploration
  delay bound (k)        2
  schedules executed     6  (cap 200)
  unique schedules       6
  reduced away           3   (provably equivalent under independence)
  duplicate schedules    3   (already explored, never re-run)
  new findings           2     (write_write pipeline_output, first seen at k=0; write_write pipeline_output, first seen at k=1)
  coverage               bounded — Bounded search: absence of findings is not proof of absence.
```

(Two distinct findings, not one: `Finding.finding_id` is derived from the concrete evidence
`seq` pair, which differs across schedules even when the underlying race is the same class —
`build_report` records a "new" finding per distinct id, which is the honest behaviour: two
different schedules produced two different pieces of concrete evidence for the same underlying
unsynchronised write pair.)

**2. `research_fanout`-shaped: zero findings across the *entire* k=2 frontier, not just the
default schedule** — gate G2's "harder half," checked one schedule at a time:

```
Bounded schedule exploration
  delay bound (k)        2
  schedules executed     13  (cap 200)
  unique schedules       13
  reduced away           43   (provably equivalent under independence)
  duplicate schedules    12   (already explored, never re-run)
  new findings           0
  coverage               bounded — Bounded search: absence of findings is not proof of absence.
```

`test_research_fanout_shaped_g2_holds_across_entire_k2_frontier` calls `detect_conflicts`
independently on each of the 13 executed schedules' own event logs and asserts every single one
returns zero findings — 13/13, not an aggregate count that could hide one bad schedule inside a
larger zero-sum.

**3. Same seed → identical explored-schedule sequence, 20/20:**
`test_same_seed_identical_explored_schedule_sequence_20_of_20` runs the `code_pipeline`-shaped
exploration 20 times and asserts every run's `canonical_delay_schedule_text` sequence is
byte-identical to the first. All 20 matched.

**4. Reduction effectiveness measured and reported (explored/pruned/duplicate)** — every
`Report` carries `schedules_executed`, `reduced_away` and `duplicate_children` as measured
integers (never estimated), and `format_report`'s text always renders all three. The
`research_fanout`-shaped run above measured 13 executed, 43 reduced away, 12 duplicates — a
concrete number for a concrete shape, not a claimed constant.

## What is not built, and why

1. **The literal `code_pipeline`/`research_fanout` fixtures were not explored.** See the
   section above — `sdk/langgraph.py` does not route LangGraph dispatch through `Scheduler`, so
   `delay_schedule` has nothing to act on for either fixture today. Wiring that is a follow-on
   prompt's scope, not silently dropped.
2. **`strategy = "random"` and `"replay_set"` are declared in config, not implemented.** PRD
   §15.1 names three strategies; only `delay_bounded` (this document's own subject) is built.
   `random` is PRD §34.4's comparison baseline for a future benchmark suite; `replay_set` replays
   an externally supplied fixed set. Both are out of P13's `DELIVERABLES`; the config field
   exists so a future prompt's surface does not collide with this one.
3. **No sleep sets or full DPOR.** See the reduction section above — this is a measured,
   reported decision (Design Constraint 3), not an oversight. Nothing in this build reads
   `upgrade_reduction_if_redundancy_over` to switch algorithms.
4. **No CLI or API surface beyond the reporting payload itself.** OUT OF SCOPE, honored
   literally: `report.to_api_payload` exists as a plain function returning a dict; wiring it into
   an actual CLI command or HTTP endpoint is a later prompt's job.
5. **No new detector, no UI.** `explore/` reuses `analysis.race.detect_conflicts` verbatim (see
   above) and renders nothing beyond `format_report`'s plain text.
6. **First OP-2 audit complete (2026-09-07), verdict PASS WITH NOTES — a second, independent
   re-audit is still owed**, same standing pattern as every module in this project. No
   functional defect was found in this module's own shipped code; see `CONTEXT.md` §9 D-88 and
   §13 for the full account, and this file's own "Error codes" section below (added by that
   audit's own finding #1).

## Error codes

<a id="e-expl-000"></a>
### `E-EXPL-000` — base class, never directly raised

`explore.schedule.ExploreError`'s own default code. Every real error `explore/` raises
constructs a subclass with its own fixed code instead (see `E-EXPL-001` below); this code
exists only as the base class's declared default and should never appear in a live error
message. If it ever does, that is itself a bug — a call site is raising `ExploreError`
directly instead of a named subclass.

<a id="e-expl-001"></a>
### `E-EXPL-001` — malformed run: no usable `schedule_decision` structure

Raised by `explore.schedule.MalformedRunError` (`turns_from_events`) when a run's event log
either contains zero `schedule_decision` events (nothing to branch on — the log likely comes
from a non-scheduler-backed execution, PRD §15.3's precondition) or contains two
`schedule_decision` events that claim the same `sched_step` with a different
`chosen_task_id` (the log is not from one coherent, replayable run). **Fix:** re-run the
target through the real `Scheduler` (`runtime.scheduler.Scheduler`) rather than handing
`explore()` a hand-assembled or corrupted event log.

Not to be confused with `api/routes/analysis.py`'s unrelated `E-EXPLORE-001` (a 409 "no
bounded-exploration report is persisted for this run yet" stub on `GET
/api/runs/{id}/exploration`) — the two originally collided on the identical string
`E-EXPL-001` until op2-audit-p13.md finding #3 caught it; the API route's code was renamed to
avoid the collision, this module's own code was left unchanged since it is the one the string
`E-EXPL-NNN` (this module's own prefix) actually belongs to.

## Worked reference: reading a `Report`

```python
from agentdx.explore import build_report, explore, format_report

result = explore(my_scheduler_executor, delay_bound_k=2, schedule_cap_n=200, time_budget_s=120.0)
report = build_report(result)
print(format_report(report))
# ... always ends with the literal I10 sentence, and `report.coverage_statement`,
# `to_api_payload(report)["coverage_statement"]` carry the same text for any other
# consumer that renders this result without going through `format_report`.
```

**Net honest statement:** this build proves bounded schedule exploration works correctly against
a real `Scheduler` — BFS ordering, the two termination-bound conflation bugs found and fixed
during this build, seeded reproducibility, independence-based reduction with its stated
empirical limitation, and the I10 coverage requirement enforced at the type level — on hand-built
scenarios shaped like the two reference fixtures. It does not yet prove this against the fixtures
themselves, and says so in every place a reader might look for that claim.
