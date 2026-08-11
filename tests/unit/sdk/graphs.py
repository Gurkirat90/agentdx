"""Small LangGraph fixtures the adapter tests instrument.

These are *not* the PRD §23 reference fixtures — those land at P05 and are the ones the gates
are measured against. These are the smallest graphs that exercise each binding: a `LastValue`
channel (no reducer, so concurrent writes are a genuine lost update), a
`BinaryOperatorAggregate` channel (`operator.add`, so concurrent writes are a benign merge),
a fan-out that makes two nodes run in the same superstep, and a sync-callable node.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


class PipelineState(TypedDict, total=False):
    """The state of the three-node pipeline fixture."""

    task: str
    plan: str
    drafts: Annotated[list[str], operator.add]
    review: str


def build_pipeline() -> object:
    """Return a compiled planner → coder → reviewer graph with one reduced channel."""

    async def planner(state: PipelineState) -> dict[str, object]:
        return {"plan": f"plan for {state.get('task', '')}"}

    async def coder(state: PipelineState) -> dict[str, object]:
        return {"drafts": [f"draft of {state.get('plan', '')}"]}

    async def reviewer(state: PipelineState) -> dict[str, object]:
        return {"review": f"reviewed {len(state.get('drafts', []))} draft(s)"}

    graph = StateGraph(PipelineState)
    graph.add_node("planner", planner)
    graph.add_node("coder", coder)
    graph.add_node("reviewer", reviewer)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "coder")
    graph.add_edge("coder", "reviewer")
    graph.add_edge("reviewer", END)
    return graph.compile()


def build_fanout() -> object:
    """Return a graph whose two workers run in the same superstep and both write `drafts`."""

    async def planner(state: PipelineState) -> dict[str, object]:
        return {"plan": "shared"}

    async def worker_a(state: PipelineState) -> dict[str, object]:
        return {"drafts": ["a"]}

    async def worker_b(state: PipelineState) -> dict[str, object]:
        return {"drafts": ["b"]}

    graph = StateGraph(PipelineState)
    graph.add_node("planner", planner)
    graph.add_node("worker_a", worker_a)
    graph.add_node("worker_b", worker_b)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "worker_a")
    graph.add_edge("planner", "worker_b")
    graph.add_edge("worker_a", END)
    graph.add_edge("worker_b", END)
    return graph.compile()


def build_sync_pipeline() -> object:
    """Return a two-node graph whose nodes are plain functions, not coroutines."""

    def planner(state: PipelineState) -> dict[str, object]:
        return {"plan": "sync plan"}

    def coder(state: PipelineState) -> dict[str, object]:
        return {"drafts": [state.get("plan", "")]}

    graph = StateGraph(PipelineState)
    graph.add_node("planner", planner)
    graph.add_node("coder", coder)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "coder")
    graph.add_edge("coder", END)
    return graph.compile()


def build_bulk_reader(style: str) -> object:
    """Return a one-node graph whose node reads its whole state in one bulk operation.

    `style="dict"` uses `dict(state)`; `style="splat"` uses `{**state}`. Both are the
    idiomatic way a real node copies its input before mutating it, and both take a
    C-level fast path in CPython when the object is a `dict` subclass — which is the
    condition under which a recording view that subclasses `dict` records nothing.
    """

    async def reader(state: PipelineState) -> dict[str, object]:
        snapshot = dict(state) if style == "dict" else {**state}
        return {"plan": f"read {len(snapshot)} key(s)"}

    graph = StateGraph(PipelineState)
    graph.add_node("reader", reader)
    graph.add_edge(START, "reader")
    graph.add_edge("reader", END)
    return graph.compile()


def build_with_subgraph() -> object:
    """Return a graph with a compiled subgraph mounted as one of its nodes.

    A subgraph is not one agent: its own nodes are separate agents whose spans, state
    accesses and handoffs the adapter's node-binding walk never sees. Recording it as a
    single opaque node would produce a log that looks complete and is not.
    """

    async def inner(state: PipelineState) -> dict[str, object]:
        return {"plan": "planned inside the subgraph"}

    inner_graph = StateGraph(PipelineState)
    inner_graph.add_node("inner", inner)
    inner_graph.add_edge(START, "inner")
    inner_graph.add_edge("inner", END)

    async def coder(state: PipelineState) -> dict[str, object]:
        return {"drafts": [f"draft of {state.get('plan', '')}"]}

    graph = StateGraph(PipelineState)
    graph.add_node("planning", inner_graph.compile())
    graph.add_node("coder", coder)
    graph.add_edge(START, "planning")
    graph.add_edge("planning", "coder")
    graph.add_edge("coder", END)
    return graph.compile()
