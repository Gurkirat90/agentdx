# code_pipeline

Four agents (`planner` → `coder`, `reviewer` → `tester`) over shared state, with a **seeded
lost update** on `draft.module_a`. Gate G1 requires at least one `state_conflict/write_write`
finding of severity `critical` naming both writes. Built at P05 (ADR-001).

## What it models

`planner` decomposes a refactor task and dispatches `coder` and `reviewer` **concurrently**
(PRD §23.1). Both independently produce a revision of `draft.module_a`; `coder` also writes
`draft.module_b` (uncontested) and `reviewer` writes `review_notes` (uncontested). `tester`
reads whichever `draft.module_a` survived, runs the (simulated, deterministic) test suite, and
reports success regardless — PRD §45.1's point exactly: *"The bug is invisible in a trace
viewer: both nodes succeed, the run reports success, and the output is merely sometimes
wrong."*

## Why explicit state, not a LangGraph channel

PRD §23.1 and §45.1 both describe the defect as "the channel has no reducer... last writer
wins... silently discards" a LangGraph channel write. Tested against the pinned LangGraph
(`>=1.2,<1.3`, ADR-003) directly:

```
InvalidUpdateError: At key 'draft_module_a': Can receive only one value per step.
Use an Annotated key to handle multiple values.
```

The pinned LangGraph does **not** silently last-write-wins an unreduced channel — it refuses
the update outright. Two ways to reproduce the PRD's described defect were considered:

1. **Declare a trivial `Annotated[str, lambda old, new: new]` reducer.** This *does* reproduce
   silent last-write-wins (verified: the pinned LangGraph's Pregel applies concurrent updates
   to a `BinaryOperatorAggregate` channel in a deterministic, node-name-sorted order — the
   lexicographically last node name wins, so naming them `coder`/`reviewer` already gives
   `reviewer` the win, matching the PRD's narrative with no renaming needed). Rejected: the
   channel would carry a **non-null** `reducer` field on every `state_write`
   (`fixtures.code_pipeline.graph.<lambda>` or similar), which is indistinguishable, from the
   log alone, from `research_fanout`'s *genuine*, safe, accumulating reducer on `findings`
   (PRD §14.7 guard G3: a declared reducer suppresses a conflict). The eventual race detector
   (P12, `NOT STARTED`) would have to statically prove a registered reducer is
   information-discarding rather than merging before it may still flag a conflict underneath
   one — a real capability, but one this prompt cannot verify against code that does not
   exist yet, and a wrong assumption here would silently fail gate G1 once P12 lands.
2. **Route `draft.module_a` (and the fixture's other PRD §23.1 "State keys") through
   `agentdx.state()` instead of the graph's own state schema.** This is documented, group-3
   SDK surface (`docs/sdk.md` §2: "Explicit state access — only when state is not a LangGraph
   channel"). It sidesteps LangGraph's channel machinery entirely, so there is no
   `InvalidUpdateError` to route around and no reducer of any kind is ever declared for this
   key. Every `state_write` to `draft.module_a` in the golden log carries `reducer: null` and
   `lock_id: null`, unambiguously — the exact signal PRD §23.1 describes, with no dependence
   on how a not-yet-built detector treats a declared-but-destructive reducer.

**Chosen: (2).** `PipelineState` (the graph's own LangGraph schema) carries only `task`; every
key PRD §23.1 lists as this fixture's "State keys" (`plan`, `draft.module_a`, `draft.module_b`,
`review_notes`, `test_results`) is written and read through `agentdx.state()` from inside each
node — which the LangGraph adapter still attributes correctly, because binding 1 opens each
node's agent span before the node body runs (`docs/sdk.md` §3).

## Why the ordering is deterministic, not merely usually-reproducible

Design constraint 2 (this prompt) requires the race to be "deterministic and reachable... not
only under a rare interleaving," and gate G1's test expectation is "reproduction scenario
re-triggers 10/10." There is no scheduler yet (P06, `NOT STARTED`) to seed an interleaving
with. Instead, ordering is controlled the only way available at this layer: in the node
bodies themselves.

`coder` performs its `draft.module_a` write with no `await` before it (beyond the tool calls
it itself makes, which are all served synchronously from the committed response pool — no
real I/O, no uncontrolled suspension). `reviewer` opens with exactly one
`await asyncio.sleep(0)`, a single cooperative yield to the event loop. Under `asyncio`'s
single-threaded, cooperative scheduling (LangGraph's async engine runs concurrent branches of
one Pregel superstep via `asyncio.gather`, each in its own `Task`, each inheriting a copy of
the context active when the branches forked), that one yield is sufficient for `coder`'s
synchronous-until-suspension prefix to have already completed its write. Verified 10/10 in
`tests/golden/test_fixtures_replay.py` and empirically 5/5 and 10/10 during this fixture's
construction (see `docs/fixtures.md`) — `reviewer`'s write always lands second and always
survives.

This is a real property of `asyncio` + this fixture's specific code, not a coin flip that
happened to come up the same way ten times. It is also honestly fragile in one sense worth
naming: it depends on neither node performing an *additional*, earlier `await` that could
itself suspend before the intended point. Both node bodies are short and every `await` in them
is enumerated above for exactly this reason.

## What is provisional (ADR-001)

This fixture was executed through `fixtures/_harness.py`, a fixture-local, non-`src/agentdx`
stand-in for the real `runtime/` scheduler (P06) and `runtime/cache/` (P07) — neither exists
yet. Everything about event *validation*, *persistence* and the *hash chain* is real (P02 +
P03, both `BUILT`); only the *stamping* (seq/vclock assignment) and the tool-response
"cache" (a committed JSON pool, `cache/responses.json` — see `fixtures/_harness.py`'s
`ResponsePool` docstring for why this fixture needs no LLM cache at all: PRD §23.1's own
"Tools" row lists no LLM call) are provisional. `virtual_ts_ms` is a plain per-event counter,
not a calibrated duration model, so `golden_findings.json`'s `expected_not_yet_measurable`
block (the verdict and the speedup finding) records what PRD §23.1 expects without claiming
evidence for it — see that file, and see the next paragraph for why the coordination-
bottleneck finding is filed differently. The golden log is regenerated at P07 per ADR-001
consequence 2; this is the standing exception `AGENTS.md` §5 already carves out.

**OP-3 correction (2026-08-12).** An earlier revision of `golden_findings.json` recorded the
coordination-bottleneck finding with an empty evidence array, flagged
`"computable_from_this_log": false` — which understated the gap and violated invariant I6 (an
empty evidence array is a schema failure, not a softened finding). PRD §23.1's secondary
defect describes a heavy `coder<->reviewer` handoff; this fixture's `graph.py` has **no edge,
message or shared read connecting `coder` and `reviewer` at all** — the handoff is not
unmeasured, it is not built. `golden_findings.json` now says so directly, under
`expected_not_yet_built`, and no longer claims it as a zero-evidence finding.

## Healthy variant

PRD §23.1: after the documented fix (a real reducer, or routing both writes through the
planner), finding f1 (the lost update) disappears; the coordination-bottleneck and
negative-speedup findings remain, because they are a topology problem, not a concurrency one.
Building that second, fixed variant is out of this prompt's `DELIVERABLES`.
