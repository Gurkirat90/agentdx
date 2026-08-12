"""fixtures/support_triage/graph.py — Fixture 2 (PRD §23.2).

`classifier -> retriever_a, retriever_b -> responder`. `retriever_a` and `retriever_b` are
declared as a 2-way fan-out from `classifier`, exactly like `research_fanout`'s workers — the
point of this fixture is that they are *not* the same kind of parallel underneath (PRD §23.2
seeded defect 2), and that they duplicate work (seeded defect 1). See `README.md`.

**Zero state conflicts, by construction.** `retriever_a` and `retriever_b` never write the
same key: each writes its own `context.docs_a` / `context.docs_b`, and only `responder` (the
single downstream consumer) ever writes the merged `context.docs`. PRD §23.2 point 4 requires
this fixture to be a *partial* false-positive control — performance defects, no concurrency
defects — and a shared write target would have accidentally reintroduced a second, unseeded
race.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

import agentdx
from fixtures._harness import load_pool

FIXTURE_DIR = Path(__file__).parent
TASK = "Classify and draft a response to ticket #4821 (duplicate subscription charge)."
TICKET_TEXT = "I was charged twice for my subscription this month"
_POOL = load_pool(FIXTURE_DIR)


class TriageState(TypedDict, total=False):
    """The graph's own LangGraph state.

    Minimal — see `code_pipeline/graph.py`'s README section for why the PRD's listed "State
    keys" live in `agentdx.state()` instead.
    """

    task: str


def _key(args: dict[str, Any]) -> str:
    """Return the response-pool lookup key for a tool call's arguments."""
    return json.dumps(args, sort_keys=True)


@agentdx.tool("vector_search")
async def vector_search(query: str, k: int) -> list[str]:
    """Semantic search over the knowledge base.

    `query`/`k` are exactly what both retrievers pass — see README — which is what makes the
    two calls exact-hash identical (PRD §16.3, CONTEXT.md §3: v1 redundancy detection is
    exact-hash of `tool_name ‖ canonical_json(args)` only).
    """
    return _POOL.get("vector_search", _key({"query": query, "k": k}))


@agentdx.tool("kb_lookup")
async def kb_lookup(collection: str) -> list[str]:
    """Look up a fixed knowledge-base collection by name."""
    return _POOL.get("kb_lookup", _key({"collection": collection}))


@agentdx.tool("ticket_history")
async def ticket_history(ticket_id: str) -> list[str]:
    """Look up prior tickets from the same customer."""
    return _POOL.get("ticket_history", _key({"ticket_id": ticket_id}))


async def classifier(state: TriageState) -> dict[str, Any]:
    """Classify the ticket into a category and record it."""
    category = "billing.duplicate_charge"
    async with agentdx.state() as s:
        await s.write("ticket", TICKET_TEXT)
        await s.write("category", category)
    return {}


async def retriever_a(state: TriageState) -> dict[str, Any]:
    """Retrieve grounding context via `vector_search` — and, redundantly, `kb_lookup`."""
    hits = await vector_search(TICKET_TEXT, 5)
    kb = await kb_lookup("billing_docs")
    async with agentdx.state() as s:
        await s.write("context.docs_a", {"vector_search": hits, "kb_lookup": kb})
    return {}


async def retriever_b(state: TriageState) -> dict[str, Any]:
    """Retrieve grounding context via the *same* `vector_search` call as `retriever_a`.

    This is seeded defect 1 (PRD §23.2): both retrievers derive their query from the ticket
    text the same way, so the arguments are identical and the tool call is wasted work,
    exact-hash detectable.
    """
    hits = await vector_search(TICKET_TEXT, 5)
    history = await ticket_history("cust_4821")
    async with agentdx.state() as s:
        await s.write("context.docs_b", {"vector_search": hits, "ticket_history": history})
    return {}


async def responder(state: TriageState) -> dict[str, Any]:
    """Merge both retrievers' context and draft a response. The sole writer of both keys."""
    async with agentdx.state() as s:
        docs_a = await s.read("context.docs_a")
        docs_b = await s.read("context.docs_b")
        category = await s.read("category")
        await s.write("context.docs", {"a": docs_a, "b": docs_b})
        await s.write(
            "response_draft",
            f"Category: {category}. We found a duplicate charge and are issuing a refund.",
        )
    return {}


def build_graph() -> agentdx.InstrumentedGraph:
    """Compile and instrument the graph. Returns the object `agentdx.run()` invokes."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(TriageState)
    builder.add_node("classifier", classifier)
    builder.add_node("retriever_a", retriever_a)
    builder.add_node("retriever_b", retriever_b)
    builder.add_node("responder", responder)
    builder.add_edge(START, "classifier")
    builder.add_edge("classifier", "retriever_a")
    builder.add_edge("classifier", "retriever_b")
    builder.add_edge("retriever_a", "responder")
    builder.add_edge("retriever_b", "responder")
    builder.add_edge("responder", END)
    compiled = builder.compile()
    return agentdx.instrument(compiled, name="support_triage")


__all__ = [
    "FIXTURE_DIR",
    "TASK",
    "build_graph",
    "classifier",
    "kb_lookup",
    "responder",
    "retriever_a",
    "retriever_b",
    "ticket_history",
    "vector_search",
]
