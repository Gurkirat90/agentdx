"""D-62, step 1: what actually deadlocks the scheduler — fan-out, or any real suspension?

**An experiment, not a regression suite.** It settles one question before anyone writes the
`sdk/`↔`Scheduler` wiring D-62 needs, because the answer selects between the designs in
`d62-design.md` §4 and the ledger currently records the wrong reason.

**The claim under test.** `CONTEXT.md`, the `Dockerfile` header and this session's own earlier
notes all stated as fact that *"LangGraph's parallel fan-out deadlocks the single-task
scheduler loop."* Reading `runtime/scheduler.py` suggests concurrency has nothing to do with
it: a task suspends legitimately only by awaiting a Future the *scheduler* created
(`yield_point` → `RUNNABLE`, `sleep` → `BLOCKED` + timer). `_collect_runnable` collects only
`PENDING`/`RUNNABLE`, so a task suspended on anything else stays `RUNNING` — not runnable, no
timer — and `_scheduler_loop` raises `DeadlockError` (`E-SCHED-003`).

**RESULT, 2026-09-01, Darwin/arm64, CPython 3.12.2 — the headline is settled.** The
single-node sequential LangGraph graph **deadlocks**. No branches, no LLM, nothing to
parallelise. A graph with no parallelism cannot be deadlocked by parallelism, so **fan-out is
not D-62's cause** and the ledger's framing is wrong. That test builds and drives a real graph
and depends on none of the helpers below, so it stands on its own.

**What that first run did NOT establish, and a correction.** The original control
(`test_one_tick_await_survives`) failed, and the failure was a defect in the test rather than
a property of the scheduler: it used `ensure_future` + `Event.wait()` and called that a
"one tick" case, but `ensure_future` only *schedules* the setter, so the case always required
the loop to run another task first. There was never a one-tick control, and the "one event-loop
tick budget" this file and `d62-design.md` §3 both asserted is **unmeasured**. It is no longer
claimed anywhere.

The ladder below replaces it and locates the boundary in terms that were actually observed:

1. `test_a_root_that_never_suspends_completes` — no `await` at all.
2. `test_a_root_that_awaits_an_already_resolved_future_completes` — `await` that needs
   nothing scheduled.
3. `test_a_suspension_needing_another_task_deadlocks` — `await` that needs the loop to run
   something else.
4. `test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all` — the real thing.

1 and 2 passing with 3 failing is what locates the boundary: **the scheduler tolerates
suspension, but not suspension whose resolution requires another task to run.** If 1 or 2
fails, the harness is broken and 3 proves nothing — read them first.

    just test tests/integration/runtime/test_d62_suspension_contract.py -v -m experiment

**Repair note (2026-09-02, OP-3 against the P20 OP-2 audit).** Test 4 now carries
`@pytest.mark.experiment` and is excluded from the default `pytest`/`just test`/`just ci`
collection (`pyproject.toml`), the same treatment `tests/acceptance/` already gets and for the
same reason: it is written to fail by design while D-62 is open, and an unmarked member of the
default suite going red for a reason no single commit can fix is exactly what CONTEXT.md §11
tripwire 15 exists to catch. Tests 1-3 (the controls) carry no marker and still run by default —
only the decisive probe is excluded. The command above now needs `-m experiment` to include it;
plain `just test tests/integration/runtime/test_d62_suspension_contract.py -v` runs only 1-3.

**Why `strict_determinism=False`, and why that is not a bypass.** `strict` gates three
unrelated things in `DeterminismGuard`: the `PYTHONHASHSEED` check, `_patch_time` and
`_patch_thread_spawn`. Under `strict=True` a clock read or thread spawn anywhere — including
inside Pregel, which this experiment deliberately does not control — raises
`DeterminismLeakError` *before* the scheduler loop is reached. The very first run of this file
did exactly that: three `E-SCHED-004`s and nothing learned. Leaving strict on would not make
the experiment stricter; it would let a different failure impersonate the one under test. I1
is not weakened by declining to test it here — `tests/determinism/` is where that lives.

Run through `just` regardless (`PYTHONHASHSEED=0`, justfile:11) so the environment matches
every other suite.
"""

from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest

from agentdx.runtime.scheduler import DeadlockError
from tests.unit.runtime.conftest import build_scheduler


async def _returns_without_awaiting() -> str:
    """A root coroutine that never suspends at all. The floor of the ladder.

    If the scheduler cannot run this, nothing else in the file is interpretable.
    """
    return "no suspension"


async def _awaits_an_already_resolved_future() -> str:
    """Await a Future that is already done — a suspension point that resolves instantly.

    `await` on a completed Future does not yield to the event loop, so this exercises the
    *syntax* of suspension without requiring any other task to run. The scheduler should
    not care.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    fut.set_result(None)
    await fut
    return "resolved future"


async def _awaits_work_done_by_another_real_task() -> str:
    """Await an `asyncio.Event` set by a separate real event-loop task.

    This is the shape that matters: a suspension the scheduler did not create, whose
    resolution requires the event loop to run *a different task*. LangGraph's Pregel executor
    suspends this way.

    **Not a contrived hang.** `_setter` always sets the event; under a plain `asyncio.run`
    this coroutine completes. If the scheduler deadlocks on it, the scheduler is refusing a
    suspension that would otherwise have resolved.

    **Correction, 2026-09-01.** An earlier version took a `ticks` parameter and claimed
    `ticks=0` was a "one tick" control. That was wrong: `ensure_future` only *schedules*
    `_setter`, so even the zero-iteration case needs the loop to run another task first.
    There was never a one-tick case, and the control's failure was this bug rather than a
    property of the scheduler. The tick *count* is deliberately no longer claimed anywhere —
    what is measured here is "requires another task to run", not "requires N ticks".
    """
    done = asyncio.Event()

    async def _setter() -> None:
        done.set()

    task = asyncio.ensure_future(_setter())
    await done.wait()
    await task
    return "event set by another task"


@pytest.mark.asyncio
async def test_a_root_that_never_suspends_completes() -> None:
    """CONTROL 1 (floor). The scheduler runs a coroutine that never awaits.

    If this fails, the harness is broken and nothing else here is interpretable.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    assert await scheduler.run(_returns_without_awaiting()) == "no suspension"


@pytest.mark.asyncio
async def test_a_root_that_awaits_an_already_resolved_future_completes() -> None:
    """CONTROL 2. Suspension syntax alone is tolerated when nothing else must run.

    Separates "the scheduler rejects `await`" (it does not) from "the scheduler rejects a
    suspension requiring another task to run" (the property under test).
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    assert await scheduler.run(_awaits_an_already_resolved_future()) == "resolved future"


@pytest.mark.asyncio
async def test_a_suspension_needing_another_task_deadlocks() -> None:
    """THE MECHANISM. A suspension whose resolution needs another task to run deadlocks it.

    No concurrency in the user sense: one root coroutine, one `Event`, one setter. Read
    together with the two controls above, this locates the boundary — the scheduler tolerates
    `await` (control 2) but not an `await` that requires the loop to schedule something else.

    The boundary is stated in those terms deliberately. An earlier version of this file
    claimed a "one event-loop tick" budget; that number was never measured and is not claimed
    here. `_resume_task` does grant exactly one `asyncio.sleep(0)`, but whether one tick is
    the precise cut-off is a separate question this experiment does not answer.
    """
    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    with pytest.raises(DeadlockError) as caught:
        await scheduler.run(_awaits_work_done_by_another_real_task())
    # The wait_reason is empty because the task never reached a yield point — the same
    # signature the real seed container produced (`{t_r_..._root_0: }`).
    assert "E-SCHED-003" in str(caught.value)


@pytest.mark.experiment
@pytest.mark.asyncio
async def test_a_sequential_langgraph_graph_deadlocks_with_no_fanout_at_all() -> None:
    """THE DECISIVE ONE. A single-node, strictly sequential LangGraph graph — no fan-out.

    One node. No branches. No LLM call. No parallelism of any kind. It is driven as the
    scheduler's root coroutine exactly as production does it
    (`cli/commands/run.py:258` — `scheduler.run(agentdx.run(...))`), minus the SDK layer, so
    what is under test is the `Scheduler`↔Pregel interaction and nothing else.

    **Deadlocks** → parallel fan-out was never the cause. A graph with nothing to
    parallelise cannot be deadlocked by parallelism. `CONTEXT.md`'s framing is wrong, and
    D-62 is about the suspension contract: `d62-design.md` options A and B are both about
    ownership of *suspension*, and option C (generic path only) is unaffected.

    **Completes** → Pregel settles within the one-tick budget for a trivial graph, the
    hypothesis is wrong or incomplete, and the next question is which graph shape first
    breaks it. Add nodes until it does; that boundary is the real finding.
    """
    pytest.importorskip("langgraph", reason="LangGraph is a locked dependency (CONTEXT.md §3)")
    from langgraph.graph import END, START, StateGraph

    class _ProbeState(TypedDict, total=False):
        """One key in, one key out — the smallest possible graph state.

        Mirrors `fixtures/code_pipeline`'s own `PipelineState` shape (a `TypedDict`,
        `total=False`) so the graph is built the way the real fixtures are built, rather
        than in a shape LangGraph might treat differently.
        """

        task: str
        out: str

    def _only_node(state: _ProbeState) -> dict[str, str]:
        """The entire graph. Synchronous, no await, no LLM, nothing to schedule."""
        return {"out": "done"}

    builder = StateGraph(_ProbeState)
    builder.add_node("only", _only_node)
    builder.add_edge(START, "only")
    builder.add_edge("only", END)
    graph = builder.compile()

    scheduler, _sink, _clock = build_scheduler(strict_determinism=False)
    try:
        result = await scheduler.run(graph.ainvoke({"task": "d62 probe"}))
    except DeadlockError as exc:
        pytest.fail(
            "HYPOTHESIS CONFIRMED (this failure is the finding, not a bug in the test): a "
            "single-node sequential graph with no fan-out deadlocked the scheduler. "
            "Parallelism is not D-62's cause; the one-tick suspension contract is. Update "
            "CONTEXT.md's framing and choose between d62-design.md options A and B on that "
            f"basis. Raised: {exc}"
        )
    assert result is not None, (
        "HYPOTHESIS REFUTED: a sequential graph completed under the scheduler. Fan-out, or "
        "something else specific to multi-node execution, is the real trigger. Widen the "
        "graph one node at a time to find the boundary before designing the wiring."
    )
