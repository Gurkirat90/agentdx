"""A fixed, nontrivial multi-agent-shaped scenario for gate G3.

Not a `DELIVERABLES` module (it is test infrastructure, not `src/agentdx/`), but it is the
one place gate G3's scenario is defined, so both the in-process test
(`test_replay_equality.py`) and the fresh-subprocess runner (`_subprocess_runner.py`) drive
*exactly* the same task graph. If the two ever drifted, "10 of the 100 runs happen in fresh
processes" would silently stop testing the same claim as the other 90.

The scenario itself uses only fake tasks — no LLM, no graph, no fixture — per design
constraint 6, but it deliberately exercises more of the scheduler's surface than any single
`tests/unit/runtime/test_scheduler.py` test does in one place: four agents with different
step counts and sleep durations (so the seeded tie-break at equal `virtual_ready_ts_ms` and
the clock-advance-on-nothing-runnable path both fire), plus non-scheduler-authored events
(`TOOL_CALL`, `ASSERTION_RESULT`, `INSTRUMENTATION_GAP`) stamped mid-run through
`Scheduler.stamp` — the same path the SDK would use — so the canonical projection compared
across replays reflects real payload content, not just `schedule_decision` bookkeeping.
"""

from __future__ import annotations

import asyncio

from agentdx.config import SchedulerConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.scheduler import Scheduler
from tests.unit.events.factories import sample_payload
from tests.unit.runtime.conftest import MemorySink

RUN_ID = "r_g3demo"

# (agent_id, step_count, sleep_ms) — fixed, not derived from the seed. Only the scheduler's
# *choice* among these varies with the seed; the scenario shape itself must not.
_AGENTS: tuple[tuple[str, int, int], ...] = (
    ("planner", 3, 0),
    ("coder", 2, 150),
    ("reviewer", 4, 50),
    ("tester", 1, 300),
)


async def _worker(scheduler: Scheduler, agent_id: str, span_id: str, n_steps: int) -> str:
    """Yield `n_steps` times, stamping one `TOOL_CALL` per step, then report done."""
    for i in range(n_steps):
        await scheduler.yield_point(f"{agent_id}_step_{i}")
        scheduler.stamp(
            DraftEvent(
                type=EventType.TOOL_CALL,
                payload=sample_payload(EventType.TOOL_CALL, salt=i),
                agent_id=agent_id,
                clock_slot=agent_id,
                span_id=span_id,
            )
        )
    return agent_id


async def _agent(scheduler: Scheduler, agent_id: str, n_steps: int, sleep_ms: int) -> None:
    """One fake agent: run its worker steps, then optionally sleep, then assert done."""
    span_id = f"span_{agent_id}"
    await _worker(scheduler, agent_id, span_id, n_steps)
    if sleep_ms:
        await scheduler.sleep(sleep_ms)
    scheduler.stamp(
        DraftEvent(
            type=EventType.ASSERTION_RESULT,
            payload=sample_payload(EventType.ASSERTION_RESULT, salt=len(agent_id)),
        )
    )


async def _root(scheduler: Scheduler) -> list[str]:
    """Spawn all four fake agents, stamp a message pair and one gap marker, return their ids."""
    for agent_id, n_steps, sleep_ms in _AGENTS:
        scheduler.spawn(_agent(scheduler, agent_id, n_steps, sleep_ms), agent_id=agent_id)

    # A fixed, seed-independent `causes=`-bearing pair: `planner` sends, `coder` receives.
    # This is the only place in gate G3 that exercises the causal-merge half of PRD §14.2 —
    # without it, every event in this scenario falls back to the linear-chain default and a
    # vclock-merge bug in `Scheduler._compute_vclock` would never be reached by G3 at all.
    send_event = scheduler.stamp(
        DraftEvent(
            type=EventType.MESSAGE_SEND,
            payload=sample_payload(EventType.MESSAGE_SEND, salt=99),
            agent_id="planner",
            clock_slot="planner",
            span_id="span_planner",
        )
    )
    scheduler.stamp(
        DraftEvent(
            type=EventType.MESSAGE_RECV,
            payload=sample_payload(EventType.MESSAGE_RECV, salt=99),
            agent_id="coder",
            clock_slot="coder",
            span_id="span_coder",
        ),
        causes=(send_event.seq,),
    )

    scheduler.stamp(
        DraftEvent(
            type=EventType.INSTRUMENTATION_GAP,
            payload=sample_payload(EventType.INSTRUMENTATION_GAP),
        )
    )
    return [agent_id for agent_id, _, _ in _AGENTS]


def build_scenario_scheduler(seed: int) -> tuple[Scheduler, MemorySink]:
    """Build a fresh `Scheduler` (and its in-memory sink) for the fixed scenario at `seed`."""
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(RUN_ID, sink, batch_size=1)
    config = SchedulerConfig(strict_determinism=True, step_budget=100_000)
    scheduler = Scheduler(
        run_id=RUN_ID,
        seed=seed,
        clock=clock,
        writer=writer,
        config=config,
        policy="random",
    )
    return scheduler, sink


async def run_scenario_async(seed: int) -> tuple[Event, ...]:
    """Run the fixed scenario at `seed` to completion; return the resulting event log."""
    scheduler, sink = build_scenario_scheduler(seed)
    await scheduler.run(_root(scheduler))
    return sink.events()


def run_scenario(seed: int) -> tuple[Event, ...]:
    """Synchronous entry point for callers (e.g. the subprocess runner) with no running loop."""
    return asyncio.run(run_scenario_async(seed))
