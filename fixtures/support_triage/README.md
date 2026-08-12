# support_triage

Seeded **redundant work**: the same tool call issued by more than one agent. The only input
the exact-hash redundancy detector has, which is why it is built in week 1 rather than week
10 (ADR-001). Scope-cut #6 — but cutting it does not touch the healthy control.

## What it models

`classifier → {retriever_a, retriever_b} → responder` (PRD §23.2). `classifier` categorises
the ticket; `retriever_a` and `retriever_b` are dispatched as a declared 2-way fan-out;
`responder` merges both and drafts a reply.

## Seeded defect 1 — redundant retrieval

Both retrievers derive their `vector_search` query the same way (the ticket text, verbatim)
and call it with identical `(query, k)` arguments. Verified against a real run
(`tests/golden/support_triage.jsonl`): `tool_call` seq 9 (`retriever_a`) and seq 20
(`retriever_b`) share `args_hash`. This is exact-hash detectable by construction — CONTEXT.md
§3 fixes v1 redundancy detection at exact hash of `tool_name ‖ canonical_json(args)`, no
embedding similarity, so the fixture deliberately does **not** seed a semantic-only
duplication (two different-but-equivalent queries) that the detector is architecturally
unable to find (design constraint 3, this prompt).

Each retriever also calls one other, non-duplicated tool (`kb_lookup` / `ticket_history`) so
the redundancy is a real duplicate buried among genuine work, not the only thing either agent
does.

## Seeded defect 2 — fake parallelism

Both retrievers are children of `classifier` and nothing else; there is no edge, message or
shared write between `retriever_a` and `retriever_b` themselves — a declared 2-way fan-out
that, in the PRD's framing, does not overlap meaningfully once real timing exists. See
`golden_findings.json` for why this fixture cannot yet independently verify the *quantitative*
overlap/parallelism threshold (`fixtures/_harness.py` has no calibrated duration model —
Q-43.2.2 — until P06 lands).

**OP-3 correction (2026-08-12).** That timing gap is not the whole story. The *qualitative*
"fake fan-out" distinction PRD §23.2 asks for — this fan-out being structurally
distinguishable from a genuine one — is not built at all: `retriever_a`/`retriever_b`'s
topology (N children of one node, no edges between them, one downstream consumer) is
identical to `research_fanout`'s genuine fan-out. Nothing in the code differentiates "fake"
from "real" here beyond having two branches instead of four and one redundant call.
`golden_findings.json` previously flagged this `"computable_from_this_log": false`, which
understated it as a measurement gap; it is now filed under `expected_not_yet_built`, naming
the structural gap directly (invariant I6).

## Zero state conflicts, verified

`retriever_a` writes only `context.docs_a`; `retriever_b` writes only `context.docs_b`;
`responder` is the sole writer of the merged `context.docs` and of `response_draft`;
`classifier` is the sole writer of `ticket` and `category`. No key in
`tests/golden/support_triage.jsonl` has more than one writer — PRD §23.2 point 4 requires
this fixture to be a *partial* false-positive control (performance defects, no concurrency
defects), proving the redundancy/fan-out detectors and the race detector are independent
rather than one alarm wired to everything.

## What is provisional

Same standing exception as `code_pipeline` (ADR-001): stamping and tool responses come from
`fixtures/_harness.py` and a committed JSON pool, not from `runtime/` or `runtime/cache/`
(neither exists yet). See `code_pipeline/README.md` "What is provisional" for the full
argument — it applies here unchanged.
