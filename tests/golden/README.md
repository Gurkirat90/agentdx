# tests/golden/

Golden event logs and findings files. Regenerated **only on an explicit written instruction**, and
the regenerating commit states what changed underneath them and why (AGENTS.md §5). Standing
exception: the week-1 fixture corpora are provisional and are regenerated at P07 (ADR-001).

## The P05 fixture corpora

| File | What it is |
|---|---|
| `event_log_40.jsonl`, `build_event_log_40.py` | P02's hand-specified, clock-derived analyser fixture — unrelated to the three P05 reference fixtures below. |
| `code_pipeline.jsonl`, `support_triage.jsonl`, `research_fanout.jsonl` | Canonical JSONL event logs (`agentdx.events.canonical.encode_event`) for the three `fixtures/` reference systems, recorded at seed 42 through `fixtures/_harness.py` (PRD §23, FR-12). |
| `fixtures_runner.py` | The comparison harness. `python -m tests.golden.fixtures_runner check` re-runs all three fixtures and compares `canonical_log_hash` against the files above — the same function gate G3 uses, so the comparison is already I11-safe (volatile fields excluded) with no bespoke diff logic. `... regenerate` overwrites them; the standing exception above is what makes that not a violation of AGENTS.md §5's "explicit instruction" rule. |
| `test_fixtures_replay.py` | The same checks, pytest-collected, plus the structural assertions each fixture's own `README.md` promises: gate G1's lost-update evidence (deterministic 10/10, `reducer: null` on both writes, no causal edge between them), gate G2's empty finding set and `research_fanout`'s reducer-based race-freedom argument, and `support_triage`'s exact-hash redundancy and zero-state-conflicts checks. |

**Why checks run before `run_end`, not after (PRD §21.6 says "after the run is sealed").**
`EventWriter` refuses any write once `run_end` has been written (`E-EVENT-050`; PRD §9.7,
append-only), so an `assertion_result` genuinely written *after* the log is sealed could never
land in that log — yet PRD §21.6 also says the result is "part of the log and therefore part
of the evidence." Read as "after the run's own activity has produced its final state" rather
than "after `EventWriter.seal()`," both are satisfiable at once: `fixtures/_harness.py`'s
`FixtureRunHost.close_run` runs every check against the graph's final output, emits one
`assertion_result` per outcome, and only then emits `run_end`. See
`fixtures/code_pipeline/checks.py` for what a check receives and returns.
