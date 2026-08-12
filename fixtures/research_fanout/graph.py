"""fixtures/research_fanout/graph.py — Fixture 3, the healthy control (PRD §23.3).

`supervisor -> worker_1..4 -> synthesiser`. **The structural race-freedom argument is written
first, in `README.md`, before this code — read it there.** In one sentence: the only key any
two workers both touch is `findings`, and it is a genuine LangGraph reducer channel
(`Annotated[list[str], operator.add]`), so every worker's contribution is preserved regardless
of completion order; no worker ever reads a value another worker wrote; no lock, message or
transaction is needed because there is nothing to arbitrate.
"""

from __future__ import annotations

import json
import operator
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypedDict

import agentdx
from fixtures._harness import load_pool

FIXTURE_DIR = Path(__file__).parent
TASK = "What are the main approaches to deterministic replay for distributed systems?"
SUBTOPICS = (
    "record/replay logging",
    "deterministic scheduling",
    "state-machine replication",
    "checkpoint/restart",
)
_POOL = load_pool(FIXTURE_DIR)


class ResearchState(TypedDict, total=False):
    """The graph's own LangGraph state.

    `findings` is the **one** key more than one node writes, and it is declared with a real,
    information-preserving reducer — list concatenation. This is not a LangGraph technicality
    worked around, the way `code_pipeline` works around one (see that fixture's README); it
    is the mechanism this fixture exists to exhibit correctly. `question`, `subtopics` and
    `report` each have exactly one writer and live in `agentdx.state()` instead, matching the
    other two fixtures' convention.
    """

    task: str
    findings: Annotated[list[str], operator.add]


def _key(args: dict[str, Any]) -> str:
    """Return the response-pool lookup key for a tool call's arguments."""
    return json.dumps(args, sort_keys=True)


@agentdx.tool("web_search")
async def web_search(query: str) -> list[str]:
    """A replayed web search (PRD §23.3: "web_search (replayed)")."""
    return _POOL.get("web_search", _key({"query": query}))


@agentdx.tool("summarise")
async def summarise(topic: str, hits: list[str]) -> str:
    """Summarise search results into one finding."""
    return _POOL.get("summarise", _key({"topic": topic, "hits": hits}))


async def supervisor(state: ResearchState) -> dict[str, Any]:
    """Divide the question into four disjoint subtopics. The sole writer of both keys."""
    async with agentdx.state() as s:
        await s.write("question", TASK)
        await s.write("subtopics", list(SUBTOPICS))
    return {}


def _make_worker(index: int) -> Callable[[ResearchState], Awaitable[dict[str, Any]]]:
    """Return a worker node bound to subtopic `index`.

    Each worker reads only its own subtopic (via the module-level `SUBTOPICS` constant — not
    a shared state read, so there is no read-after-write dependency on `supervisor` to reason
    about) and writes only `findings`, through the reducer, and nothing else.
    """

    async def worker(state: ResearchState) -> dict[str, Any]:
        topic = SUBTOPICS[index]
        hits = await web_search(topic)
        finding = await summarise(topic, hits)
        return {"findings": [finding]}

    worker.__name__ = f"worker_{index + 1}"
    return worker


worker_1 = _make_worker(0)
worker_2 = _make_worker(1)
worker_3 = _make_worker(2)
worker_4 = _make_worker(3)


async def synthesiser(state: ResearchState) -> dict[str, Any]:
    """Combine every worker's finding into a report.

    Reads the reducer-merged `findings` from LangGraph's own state (never from
    `agentdx.state()` — `findings` was never written there), and is the sole writer of
    `report`.
    """
    findings = state.get("findings", [])
    async with agentdx.state() as s:
        await s.write(
            "report",
            f"{len(findings)}/4 subtopics covered: " + " | ".join(findings),
        )
    return {}


def build_graph() -> agentdx.InstrumentedGraph:
    """Compile and instrument the graph. Returns the object `agentdx.run()` invokes."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ResearchState)
    builder.add_node("supervisor", supervisor)
    builder.add_node("worker_1", worker_1)
    builder.add_node("worker_2", worker_2)
    builder.add_node("worker_3", worker_3)
    builder.add_node("worker_4", worker_4)
    builder.add_node("synthesiser", synthesiser)
    builder.add_edge(START, "supervisor")
    for name in ("worker_1", "worker_2", "worker_3", "worker_4"):
        builder.add_edge("supervisor", name)
        builder.add_edge(name, "synthesiser")
    builder.add_edge("synthesiser", END)
    compiled = builder.compile()
    return agentdx.instrument(compiled, name="research_fanout")


__all__ = [
    "FIXTURE_DIR",
    "SUBTOPICS",
    "TASK",
    "build_graph",
    "summarise",
    "supervisor",
    "synthesiser",
    "web_search",
    "worker_1",
    "worker_2",
    "worker_3",
    "worker_4",
]
