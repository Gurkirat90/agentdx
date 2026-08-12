"""fixtures/code_pipeline/graph.py — Fixture 1 (PRD §23.1, walkthrough detail in §45.1).

`planner -> {coder, reviewer} -> tester`. `coder` and `reviewer` are dispatched concurrently
by `planner` and both independently produce a revision of `draft.module_a`. **The write is
made through `agentdx.state()` (PRD §8.2 item 3), not a LangGraph channel** — see "Why
explicit state, not a LangGraph channel" in `README.md` for why, and for the empirical
LangGraph run that is the evidence for it. No lock, no transaction, no reducer arbitrates the
two writes; whichever lands second in program order simply replaces the first. That is the
seeded defect gate G1 requires: a `state_conflict/write_write` on `draft.module_a` naming both
`coder` and `reviewer`.

`coder` also writes `draft.module_b` (uncontested — nobody else touches it), and `reviewer`
writes `review_notes` (also uncontested), so both PRD §23.1 state keys exist in the log without
either one being a second race.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypedDict

import agentdx
from fixtures._harness import load_pool

FIXTURE_DIR = Path(__file__).parent
TASK = "Refactor module_a.py so normalise() handles None and its tests pass."
_POOL = load_pool(FIXTURE_DIR)


class PipelineState(TypedDict, total=False):
    """The graph's own LangGraph state. Deliberately minimal.

    `draft.module_a`, `draft.module_b`, `plan`, `review_notes` and `test_results` (PRD
    §23.1's "State keys" row) all live in the *explicit* `agentdx.state()` registry instead —
    see README. LangGraph's own state here carries only what routes the graph.
    """

    task: str


def _key(args: dict[str, Any]) -> str:
    """Return the response-pool lookup key for a tool call's arguments."""
    return json.dumps(args, sort_keys=True)


@agentdx.tool("read_file")
async def read_file(path: str) -> str:
    """Return a file's contents, from the fixture's committed response pool."""
    return _POOL.get("read_file", _key({"path": path}))


@agentdx.tool("write_draft")
async def write_draft(path: str, content: str) -> str:
    """Persist a draft's contents. Args include the content length, not the content itself.

    Args:
        path: The file being written.
        content: The full revised source. Hashed into the tool call's `args_hash` (PRD
            §16.3) via its length rather than its text, so two *different* revisions to the
            same path never collide on `args_hash` by accident, while still keeping the
            response-pool key short and readable.
    """
    return _POOL.get("write_draft", _key({"path": path, "content_len": len(content)}))


@agentdx.tool("run_tests")
async def run_tests(module: str) -> dict[str, Any]:
    """Run the module's test suite (simulated, deterministic — PRD §23.1)."""
    return _POOL.get("run_tests", _key({"module": module}))


@agentdx.tool("lint")
async def lint(module: str) -> dict[str, Any]:
    """Lint the module (simulated, deterministic)."""
    return _POOL.get("lint", _key({"module": module}))


async def planner(state: PipelineState) -> dict[str, Any]:
    """Decompose the task and dispatch `coder` and `reviewer` concurrently."""
    async with agentdx.state() as s:
        await s.write("plan", "coder and reviewer both revise module_a; coder owns module_b")
    return {}


async def coder(state: PipelineState) -> dict[str, Any]:
    """Read the module, write a revision, and race `reviewer` on `draft.module_a`.

    No `await asyncio.sleep(0)` here, deliberately: `coder` is the branch whose write must
    land *first* so that `reviewer`'s (below) lands second and survives. See README.
    """
    original = await read_file("module_a.py")
    revised = original + "# coder: added a truthiness guard\n"
    await write_draft("module_a.py", revised)
    async with agentdx.state() as s:
        await s.write("draft.module_a", "CODER_REVISION:\n" + revised)

    module_b = "def helper():\n    return 1\n"
    await write_draft("module_b.py", module_b)
    async with agentdx.state() as s:
        await s.write("draft.module_b", module_b)
    return {}


async def reviewer(state: PipelineState) -> dict[str, Any]:
    """Read the module, write a *different* revision, and clobber `coder`'s write.

    The single `await asyncio.sleep(0)` is the entire ordering mechanism this fixture needs
    (see README): it yields control back to the event loop exactly once, which is enough for
    `coder`'s synchronous-until-its-own-first-await prefix to have already completed its
    `draft.module_a` write. Every other `await` in this function is a tool call already
    backed by the response pool (no real I/O, no real delay), so it never introduces
    additional, uncontrolled interleaving.
    """
    await asyncio.sleep(0)
    original = await read_file("module_a.py")
    corrected = original + "# reviewer: guard against None explicitly\n"
    await write_draft("module_a.py", corrected)
    async with agentdx.state() as s:
        await s.write("draft.module_a", "REVIEWER_CORRECTION:\n" + corrected)
        await s.write("review_notes", "coder's guard used truthiness, not an explicit None check")
    return {}


async def tester(state: PipelineState) -> dict[str, Any]:
    """Read whichever `draft.module_a` survived, run its tests, and record the result.

    This is the "invisible in a trace viewer" half of PRD §45.1: `run_tests` reports
    `passed`, `tester` never errors, and nothing here can tell that `coder`'s edit never made
    it into the draft it just certified.
    """
    async with agentdx.state() as s:
        await s.read("draft.module_a")
    result = await run_tests("module_a")
    await lint("module_a")
    async with agentdx.state() as s:
        await s.write("test_results", result)
    return {}


def build_graph() -> agentdx.InstrumentedGraph:
    """Compile and instrument the graph. Returns the object `agentdx.run()` invokes."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(PipelineState)
    builder.add_node("planner", planner)
    builder.add_node("coder", coder)
    builder.add_node("reviewer", reviewer)
    builder.add_node("tester", tester)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "coder")
    builder.add_edge("planner", "reviewer")
    builder.add_edge("coder", "tester")
    builder.add_edge("reviewer", "tester")
    builder.add_edge("tester", END)
    compiled = builder.compile()
    return agentdx.instrument(compiled, name="code_pipeline")


__all__ = [
    "FIXTURE_DIR",
    "TASK",
    "build_graph",
    "coder",
    "lint",
    "planner",
    "read_file",
    "reviewer",
    "run_tests",
    "tester",
    "write_draft",
]
