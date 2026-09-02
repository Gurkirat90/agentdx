"""Pre-check for D-62 Option B: is Pregel's reducer write order timing-independent?

**Why this test exists.** `d62-design.md` Option B ("node bodies become scheduler tasks")
rests on an assumption its own author flagged as unverified: "whether determinism holds
depends on Pregel's own ordering being deterministic, which is an assumption to verify, not
assume." This test verifies it, for the shape that matters most: `fixtures/research_fanout`'s
own `findings: Annotated[list[str], operator.add]` reducer channel, written concurrently by
multiple workers (PRD I4 / gate G2's fixture).

**What "verify" means here.** Reading `langgraph/pregel/_algo.py::apply_writes` shows tasks are
re-sorted by path *before* their writes are grouped by channel:

    tasks = sorted(tasks, key=lambda t: task_path_str(t.path[:3]))
    ...
    for task in tasks:
        for chan, val in task.writes:
            pending_writes_by_channel[chan].append(val)

If that's the whole story, a reducer channel's fold order is a fixed function of graph
structure (task path), not of which worker's coroutine happens to finish first in wall-clock
time — which is exactly what Option B needs to be true, since spawning node bodies as
scheduler-controlled tasks changes *when* each node runs relative to the others, and I1
requires the resulting canonical projection to stay byte-identical regardless.

**The experiment.** Build a 4-worker fan-out graph shaped exactly like `research_fanout`
(one shared `operator.add` reducer channel), give each worker a randomized real delay so
completion order actually shuffles run to run, and confirm the delay does shuffle it (or the
test proves nothing) while the final reducer write order never does.

**Result, verified directly against the project's pinned LangGraph (1.2.10, per `uv.lock`):**
80 runs, 23 of the 24 possible completion orderings observed, exactly 1 final write order.
Confirms the assumption for this shape. **What this does NOT establish:** the full
correctness of an eventual Option B implementation — this only checks the one assumption its
design rests on. It also doesn't cover every reducer shape in the codebase, only the one I4's
own healthy fixture depends on. If a future LangGraph upgrade ever changes `apply_writes`'s
sort key or removes it, this test is what should catch it — that is the whole reason it is a
regression test and not a throwaway script.
"""

from __future__ import annotations

import asyncio
import operator
import random
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


class _FanoutState(TypedDict, total=False):
    """Mirrors `fixtures/research_fanout`'s own state shape: one shared reducer key."""

    task: str
    findings: Annotated[list[str], operator.add]


def _make_worker(
    name: str, rng: random.Random
) -> Callable[[_FanoutState], Awaitable[dict[str, list[str]]]]:
    """Build a worker node that writes one distinguishable value after a random real delay.

    The delay is real wall-clock time (`asyncio.sleep`), not virtual — this test runs plain
    LangGraph directly, outside any AgentDX scheduler, specifically to observe LangGraph's own
    behaviour undisturbed.
    """

    async def _worker(state: _FanoutState) -> dict[str, list[str]]:
        await asyncio.sleep(rng.uniform(0.0, 0.08))
        return {"findings": [f"finding-from-{name}"]}

    _worker.__name__ = f"worker_{name}"
    return _worker


def _build_fanout_graph(rng: random.Random) -> CompiledStateGraph:
    """A 4-worker fan-out into one `operator.add` reducer — `research_fanout`'s own shape."""
    builder = StateGraph(_FanoutState)
    for name in ("1", "2", "3", "4"):
        builder.add_node(f"worker_{name}", _make_worker(name, rng))
        builder.add_edge(START, f"worker_{name}")
        builder.add_edge(f"worker_{name}", END)
    return builder.compile()


async def _run_once(
    graph: CompiledStateGraph, rng: random.Random
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Run the graph once; return (real completion order, final reducer write order)."""
    completion_order: list[str] = []
    async for update in graph.astream({"task": "probe", "findings": []}, stream_mode="updates"):
        completion_order.extend(name for name in update if name.startswith("worker_"))
    final_state = await graph.ainvoke({"task": "probe", "findings": []})
    return tuple(completion_order), tuple(final_state["findings"])


@pytest.mark.asyncio
async def test_reducer_write_order_is_independent_of_real_completion_timing() -> None:
    """THE ASSUMPTION D-62 Option B RESTS ON. See module docstring for the full argument.

    Fails (assertion 1) if the randomized delay didn't actually scramble completion order —
    in which case this test proves nothing and needs a wider delay range or more runs, not a
    trust in a green result. Fails (assertion 2, the real one) if the final write order to the
    shared reducer channel ever varies despite that — which would mean Option B's "spawning
    node bodies as scheduler tasks won't break I1's byte-identical canonical projection"
    premise is false, and Option B needs to be reconsidered before it is built, not patched.
    """
    n_runs = 80
    completion_orders: set[tuple[str, ...]] = set()
    write_orders: set[tuple[str, ...]] = set()

    for i in range(n_runs):
        rng = random.Random(i)  # noqa: S311 -- not cryptographic, seeding an experiment's real-delay jitter
        graph = _build_fanout_graph(rng)
        completion_order, findings = await _run_once(graph, rng)
        completion_orders.add(completion_order)
        write_orders.add(findings)

    assert len(completion_orders) >= 2, (
        f"INCONCLUSIVE after {n_runs} runs: real completion order never varied "
        f"({completion_orders!r}). The randomized delay didn't scramble timing, so this test "
        "proves nothing either way -- widen the delay range or run count before trusting a "
        "pass here."
    )
    assert len(write_orders) == 1, (
        f"Pregel's reducer write order VARIED across {len(completion_orders)} distinct real "
        f"completion orderings: {sorted(write_orders)!r}. D-62 Option B's load-bearing "
        "assumption -- that Pregel's own write application order is a fixed function of graph "
        "structure, not real completion timing -- is FALSE for this shape. Do not build "
        "Option B on this assumption without resolving this first."
    )
