# research_fanout

**The healthy control — never cut, under any scope cut.** A correct fan-out/fan-in system
that must yield an empty race-findings set across all 100 determinism replays *and* the k=2
exploration frontier (invariant I4, gate G2, never waived). A tool that reports races in
correct code is worse than no tool.

## The structural race-freedom argument (written before the code)

`supervisor → {worker_1, worker_2, worker_3, worker_4} → synthesiser` (PRD §23.3). The claim
is that no interleaving of the four workers can produce a `state_conflict` finding. The
argument has three independent parts; each is individually sufficient to prevent the *kind*
of conflict PRD §14 detects, and all three hold simultaneously here.

**1. No shared mutable state across the concurrent branches.** Each worker reads its own
subtopic from the module-level `SUBTOPICS` tuple (`fixtures/research_fanout/graph.py`), not
from a state key another agent wrote — there is no read that could observe a partial or
racing write from a sibling. Each worker's own tool calls (`web_search`, `summarise`) are
local to that worker's span; no two workers' tool calls share an `args_hash` (their queries
are disjoint by construction — `SUBTOPICS` has four distinct entries), so there is nothing
for the redundancy detector to find either.

**2. The one key more than one node writes merges through a declared, information-preserving
reducer.** `findings: Annotated[list[str], operator.add]` is a real LangGraph channel, not
worked around the way `code_pipeline` deliberately avoids one (see that fixture's README for
why). `operator.add` on two lists is concatenation: every element either write contributed is
present in the result, regardless of which write LangGraph applies first. There is no
information for a "lost update" to lose. This is PRD §14.7 guard G3's textbook case, and it is
mechanically distinguishable in the log from `code_pipeline`'s race by one field: verified
against a real run (`tests/golden/research_fanout.jsonl`), all four workers' `state_write` to
`findings` carry `reducer: "operator.add"`; `code_pipeline`'s two writes to `draft.module_a`
carry `reducer: null`. A detector that treats "a reducer is declared" as sufficient to
suppress a conflict — rather than requiring the reducer to be a *recognised, merging* one —
would get this fixture right for the wrong reason; `CHANNEL_REDUCERS`
(`src/agentdx/sdk/langgraph.py`) already names the specific reducer, not merely its presence,
for exactly this reason.

**3. No worker reads a value another worker wrote.** `synthesiser` is the only node that reads
the merged `findings`, and it does so *after* all four workers have completed (it is their
common successor in the graph). No worker ever observes another worker's output while both
are still running — there is no cross-worker read-after-write at all, so there is no value for
two workers to disagree about even setting the reducer aside.

**Then the code, then checked against the argument.** `graph.py` was written to this
specification, not the reverse. The check against the argument is mechanical, not "it looks
fine," with one honest limitation stated here rather than overclaimed (correction, 2026-08-13,
found by an independent OP-2 audit — the prior text here named a test that does not exist and
overstated what the real one checks):
`tests/golden/test_fixtures_replay.py::test_research_fanout_has_no_shared_unreduced_writes`
reads the `state_write` events already recorded in the committed golden log
(`tests/golden/research_fanout.jsonl`) and asserts that for every key written by more than one
agent, none of those writes carries a null `reducer`. In this fixture only `findings` has more
than one writer, and every write to it carries `reducer: "operator.add"` (seq 14, 25, 36, 47).
**This is a check on one recorded run's output, not a static check on `graph.py`'s source** —
it does not itself prove no worker *could* add a hidden, conditionally-reached
`agentdx.state()` write to a shared key; it proves the run that produced this golden log didn't.
Leg 1 of the argument above ("no shared mutable state") is what actually rules that out, by
inspection of `graph.py`, not this test.

## What it models

`supervisor` divides the research question into four **disjoint** subtopics and writes
`question`/`subtopics` once (single writer, `agentdx.state()`). Each worker calls
`web_search` (replayed) and `summarise`, then returns `{"findings": [finding]}` — captured by
the LangGraph adapter's binding 2, reducer and all. `synthesiser` reads the merged list and
writes `report` (single writer, `agentdx.state()`).

## Verified, not merely argued

10/10 runs through `fixtures/_harness.py` (the same provisional stamping this prompt's other
two fixtures use — see `code_pipeline/README.md` "What is provisional" for why one is needed
at all): zero `instrumentation_gap`s, all four findings present in every run's `report`, and
`state_write.reducer == "operator.add"` on every `findings` write in every run. See
`golden_findings.json` — its empty finding set **is** the assertion this fixture exists to
make, per PRD §23.3: *"Its golden findings file must be empty above `info`, and any change
that adds a finding to it fails CI."*

## What is provisional

Same standing exception as the other two fixtures (ADR-001): stamping and tool responses come
from `fixtures/_harness.py` and a committed JSON pool, not from `runtime/` or
`runtime/cache/` (neither exists yet). One thing does **not** need P06 or P07 to be true here,
unlike the other two fixtures' timing-dependent expectations: race-freedom is a structural
property of the causality graph (no conflicting writes exist to find), not a timing
measurement, so `golden_findings.json`'s empty `findings` set is fully verifiable from this
log today. The one number PRD §23.3 lists that *is* timing-dependent — "average parallelism
≥ 3.0" — is filed under `expected_not_yet_measurable` in `golden_findings.json`, alongside
the other two fixtures' timing-dependent expectations, rather than as a zero-evidence finding
(OP-3 correction, 2026-08-12 — see `code_pipeline/README.md` for the fuller version of this
correction, which mattered more there).

**Not built here:** PRD §23.3's "Resilience under `agent_crash(worker_3)`" row requires fault
injection (`runtime/faults/`, P09, `NOT STARTED`). Out of this prompt's `DELIVERABLES` —
recorded honestly in the closing `NOT DONE / RISKS`, not silently dropped from the table.
