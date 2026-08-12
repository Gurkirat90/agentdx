# The three reference fixtures — contract and status

> Companion to `docs/event-schema.md` (the event contract) and `docs/sdk.md` (the capture
> surface). Implements PRD §23 (FR-12) and PRD §33.14 (test data strategy). Each fixture's own
> `README.md` is the primary source for its specific defect and argument; this file is the
> one-page summary `AGENTS.md` §1's opening ritual and a new session's first read.

Built at P05, all three in week 1, ahead of the PRD's own schedule — **ADR-001**
(`CONTEXT.md` §8). `research_fanout` is the false-positive control every analyser built from
week 5 onward needs to exist against; the event schema does not freeze until a fixture has
proven it sufficient (`CONTEXT.md` §7). Gate FR-12.

## Why these three fixtures needed a runtime that does not exist yet, and what filled the gap

`agentdx.run()` requires an injected `RunHost` (`docs/sdk.md` §1); the real one is `runtime/`
(P06), and the real LLM cache is `runtime/cache/` (P07). Both are `NOT STARTED`
(`CONTEXT.md` §5) — ADR-001 already anticipates this ("Week-1 fixtures run without the
scheduler or the cache... their golden corpora are provisional; they must be regenerated at
the end of P07"). `fixtures/_harness.py` is the fixture-local, non-`src/agentdx` stand-in that
makes "run end to end and emit a valid event log" true today rather than at P07: a real
`Recorder` (standard vector-clock construction, applied not reinvented) over the real,
`BUILT` `EventWriter` (P02) and `Store` (P03) — nothing about validation, the hash chain or
persistence is faked, only the *stamping*. See that module's docstring for exactly what is
honestly synthetic (`sched_step`, `virtual_ts_ms`, `wall_ts_ms`) and why none of it touches
invariant I1. **Every fixture's committed `cache/responses.json` is a deterministic tool-response
pool, not an LLM cache** — none of PRD §23's three "Tools" rows lists an LLM call, so every
agent's work in these fixtures is an instrumented `@agentdx.tool` call, and I7 (offline, no
API keys) is satisfied unconditionally rather than through `runtime/cache/`'s record/replay
modes.

## The three fixtures

| Fixture | Models | Seeded defect | Must produce | Must NOT produce |
|---|---|---|---|---|
| `code_pipeline` | `planner → {coder, reviewer} → tester` (PRD §23.1) | Write-write lost update on `draft.module_a`: both writers write through `agentdx.state()` (no LangGraph channel, no reducer, no lock — see its README for why), `reviewer`'s write deterministically lands second and survives | `state_conflict/write_write` naming `coder` and `reviewer`, severity `critical` (gate G1). Deterministic 10/10 (verified: `tests/golden/test_fixtures_replay.py::test_code_pipeline_lost_update_is_deterministic_10_of_10`) | Any causal edge between the two writes (there is none in the log — a detector that invents one is wrong) |
| `support_triage` | `classifier → {retriever_a, retriever_b} → responder` (PRD §23.2) | (1) `retriever_a`/`retriever_b` issue an identical `vector_search` call — exact `args_hash` match, verified. (2) Both are structural children of `classifier` only, no edge between them — but nothing in the code yet makes this fan-out distinguishably "fake" from a genuine one; see below | `redundant_work` on `vector_search` (gate: the redundancy detector's only seeded-redundancy input, per ADR-001 rationale (c)) — the only entry in `findings`. `fake_fanout` is filed under `expected_not_yet_built`, not `findings` (see below) | **Any `state_conflict` finding at all** — verified zero state keys have more than one writer (`test_support_triage_has_zero_state_conflicts`). PRD §23.2 point 4: this fixture is a *partial* false-positive control, proving the redundancy/fan-out and race detectors are independent |
| `research_fanout` | `supervisor → worker_1..4 → synthesiser` (PRD §23.3) | **None. The healthy control.** | **Nothing above `info`.** `golden_findings.json`'s `findings: []` is the assertion (`test_research_fanout_golden_findings_is_empty_above_info`) | Any `state_conflict` finding. `findings` is the one key all four workers write, and it merges through a real, information-preserving LangGraph reducer (`Annotated[list[str], operator.add]`) — every `state_write` to it carries `reducer: "operator.add"`, verified on all four writers across 10/10 runs. See its README, "The structural race-freedom argument," written *before* the code, then checked against it |

## What each fixture's `golden_findings.json` records, and the honesty rule it follows

**OP-3 correction (2026-08-12).** An earlier revision of this file, and of every
`golden_findings.json`, put every finding PRD §23's tables list — including the ones with no
evidence in the log yet — into a single `findings` array, distinguished only by a
self-invented `"computable_from_this_log": true/false` flag. That violated invariant I6
(*"every finding... carries evidence... an empty evidence array is a schema failure, not a
softened finding"*): a `false`-flagged entry still had `"evidence": {"seq": []}`, which is a
finding claiming no evidence, not a clearly-labeled non-finding. Caught under an OP-2 audit
and repaired under OP-3, same day.

Each `golden_findings.json` now uses three top-level arrays, and only one of them is a
finding in the schema sense:

- **`findings`** — every entry has non-empty `evidence.seq`, pointing at concrete events in
  the fixture's own golden log (the lost update, the redundant tool call). This is the only
  array `scripts/check_fixture_finding_evidence.py` and gate G1/the redundancy gate read.
- **`expected_not_yet_built`** — PRD §23 describes it, but nothing in the fixture's `graph.py`
  produces the structural signal a detector would need (`code_pipeline`'s coordination
  handoff, `support_triage`'s fake-fan-out distinction). Not a measurement gap — a code gap.
  Makes no evidence claim.
- **`expected_not_yet_measurable`** — the structural fact is real and often already evidenced
  elsewhere (e.g. `research_fanout`'s reducer proof), but the specific number PRD §23 asks for
  depends on a calibrated virtual clock that does not exist yet (`fixtures/_harness.py`'s
  `virtual_ts_ms` is a plain per-event counter — `CONTEXT.md` Q-43.2.3 is still `OPEN`), or on
  verdict/baseline scoring that is P11, `NOT STARTED`. Same reason ADR-001 calls these corpora
  provisional. Makes no evidence claim.

This is `AGENTS.md` §6's evidence discipline applied one level early: *"Where the system
cannot know something, it says so"* — now by which array an entry is filed in, not by a flag
attached to something living inside `findings`.

## The perturbation pool (PRD §11.8)

`fixtures/perturbations/{code_pipeline,support_triage,research_fanout}.json` — curated,
hand-authored confident-wrong responses, one or two per fixture. No judge model produced any
of them (invariant I13, 43.1.4): each entry states the wrong response, the actually-correct
one, and why the wrong one is plausible rather than obviously broken. **Not yet wired to
anything** — `perturb` cache mode is `runtime/cache/` (P07) and fault injection is
`runtime/faults/` (P09), both `NOT STARTED`. Committed now, per this prompt's `DELIVERABLES`,
so neither prompt has to author curated data under schedule pressure later.

## The comparison harness

`tests/golden/fixtures_runner.py` (`just fixtures-check`, or `python -m
tests.golden.fixtures_runner check`) re-runs all three fixtures and compares
`agentdx.events.canonical.canonical_log_hash` — the same function gate G3 uses — against the
committed `tests/golden/{code_pipeline,support_triage,research_fanout}.jsonl`. Canonical
hashing already excludes every `Volatility.VOLATILE` field, so the comparison is I11-safe by
construction rather than by a bespoke exclusion list. `tests/golden/test_fixtures_replay.py`
is the same checks, pytest-collected, plus the structural assertions this file's table names.
Verified during construction: a change to a real (`Volatility.STABLE`) field fails the check;
a change to text that was never stored (bodies are not captured by default — invariant I8) is
correctly invisible to it, which is a feature, not a gap.

## Known limitations (stated once here, not scattered)

1. **No timing model.** Every entry in every fixture's `expected_not_yet_measurable` array
   whose `reason` cites `virtual_ts_ms`. Resolved at P07 (ADR-001 consequence 2: these corpora
   are regenerated once the scheduler exists).
2. **No fault injection.** `research_fanout`'s PRD §23.3 resilience row
   ("`agent_crash(worker_3)` ... graceful, partial result") is not built — `runtime/faults/`
   is P09. Filed under `research_fanout/golden_findings.json`'s `expected_not_yet_built` array
   rather than silently dropped from the table.
3. **No verdict or baseline.** Every `verdict`/`speedup` entry in every `golden_findings.json`
   is in that fixture's `expected_not_yet_measurable` array — `analysis/baseline` and
   `analysis/verdict` are P11, `NOT STARTED`.
4. **Two structural gaps, not measurement gaps.** `code_pipeline`'s coordination-bottleneck
   finding and `support_triage`'s fake-fan-out distinction are filed under
   `expected_not_yet_built` — the code that would produce the signal does not exist, so no
   timing model would help either. Mislabeled `computable_from_this_log: false` in an earlier
   revision; corrected under OP-3 (2026-08-12) because that label implied "measure this later,"
   not "build this later."
5. **`fixtures/_harness.py` is provisional by design**, not an oversight — see its own
   docstring and ADR-001. It is real, executed code (every fixture in this prompt was run
   through it, 5–10 times each, during construction — not merely designed on paper), not a
   stub: `AGENTS.md` §2's "no placeholder implementations" is met by the harness's own
   correctness, and its provisionality is stated as a scope boundary, not hidden as a defect.
