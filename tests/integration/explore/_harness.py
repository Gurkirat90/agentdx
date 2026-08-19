"""Two hand-built, Scheduler-driven scenarios standing in for the real fixtures (P13).

**Why synthetic, not `code_pipeline`/`research_fanout` themselves.** `explore()`'s
`ScheduleExecutor` (`explore/generate.py`) drives a run by passing `delay_schedule` straight
to a `runtime.scheduler.Scheduler`. The two reference fixtures execute through
`sdk/langgraph.py`'s `LangGraphAdapter`, which records LangGraph node reads/writes/spans but
never routes LangGraph's own Pregel dispatch through `Scheduler.yield_point`/`spawn` — so
`delay_schedule` has nothing to act on for either fixture today. Wiring that is a
`sdk/langgraph.py` change, out of this prompt's `DELIVERABLES`. This was surfaced to, and
signed off by, the operator during P13's build: prove `explore/` for real against a
Scheduler-driven harness *shaped like* the two fixtures, rather than block on the wiring gap.
`docs/exploration.md` states this limitation plainly; nothing below pretends otherwise.

Same posture as `tests/determinism/_harness.py`/`tests/integration/faults/_harness.py`: fake
tasks, no LLM, no graph, no fixture (mission Design Constraint 6's own precedent, extended to
this prompt).

**Two scenarios:**

`research_fanout`-shaped (`make_research_fanout_executor`): three worker tasks, each a couple
of yield points then one `state_write` to a *single shared key* through a **declared reducer**
(`reducer="merge"`) — PRD §14.7 guard G3's own worked example, "concurrent writers into a
reducer channel". No causal edge is ever declared between the three writers, so every pair of
writes is concurrent per `causality.concurrent` (guard G1) — this is deliberate: the scenario
exists to prove G2 holds *because* of the declared reducer, not because the writes happen to
serialise. See the module docstring's "why finding-presence is schedule-invariant here" note.

`code_pipeline`-shaped (`make_code_pipeline_executor`): two worker tasks, each a couple of
yield points then one `state_write` to a shared key with **no** reducer and **no** lock —
`fixtures/code_pipeline`'s own shape (an unsynchronised pipeline race), producing a genuine
`write_write` finding.

**Why finding-presence is schedule-invariant here.** `analysis.race.detect_conflicts` decides
concurrency from the *causality graph* (`causality.concurrent`, a vector-clock comparison), not
from which physical interleaving the scheduler actually chose. Neither scenario below ever
declares a `causes=` edge between one writer's write and another's, so every writer's writes
stay concurrent with every other's, in every vclock, regardless of `delay_schedule` — the same
finding (or absence of one) is present at k=0 and at every deeper schedule in the frontier.
Bounded exploration's contribution here is not "finds a race no single run would" — it is
running the same P12 detector, honestly, across every executed interleaving, and reporting
that consistently rather than assuming one run generalises. `docs/exploration.md` states this
plainly; it is not something a reader should have to infer from the harness code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine

from agentdx.config import SchedulerConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.explore.schedule import DelaySchedule
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.scheduler import Scheduler
from tests.unit.runtime.conftest import MemorySink

RESEARCH_FANOUT_RUN_ID = "r_p13fanout"
CODE_PIPELINE_RUN_ID = "r_p13pipeline"

RESEARCH_FANOUT_SEED = 7
CODE_PIPELINE_SEED = 11

_FANOUT_KEY = "consensus_result"
_PIPELINE_KEY = "pipeline_output"

# Matches `explore.generate.ScheduleExecutor`'s Protocol shape structurally (a plain
# `Callable`, not the Protocol itself, so this harness has no import-time dependency on
# `explore/` beyond the `DelaySchedule` type it already needs for its own signatures).
ScheduleExecutor = Callable[[DelaySchedule], "tuple[Event, ...]"]

_RootFactory = Callable[[Scheduler], Coroutine[object, object, None]]


async def _fanout_writer(scheduler: Scheduler, worker_id: str, value_hash: str) -> None:
    """Two yield points (branching room for `explore()`), then one reducer-guarded write."""
    await scheduler.yield_point(f"{worker_id}_prep_a")
    await scheduler.yield_point(f"{worker_id}_prep_b")
    scheduler.stamp(
        DraftEvent(
            type=EventType.STATE_WRITE,
            payload={
                "key": _FANOUT_KEY,
                "value_hash": value_hash,
                "prev_value_hash": None,
                "reducer": "merge",  # PRD §14.7 guard G3 — declared reducer.
                "txn_id": None,
                "lock_id": None,
            },
            agent_id=worker_id,
            clock_slot=worker_id,
            span_id=f"span_{worker_id}",
        )
    )


async def _fanout_root(scheduler: Scheduler) -> None:
    """Spawn three concurrent writers into the same reducer-guarded channel."""
    workers = (
        ("worker_1", "vh:fanout-1"),
        ("worker_2", "vh:fanout-2"),
        ("worker_3", "vh:fanout-3"),
    )
    for worker_id, value_hash in workers:
        scheduler.spawn(_fanout_writer(scheduler, worker_id, value_hash), agent_id=worker_id)


async def _pipeline_writer(scheduler: Scheduler, worker_id: str, value_hash: str) -> None:
    """Two yield points, then one *unguarded* write — no reducer, no lock."""
    await scheduler.yield_point(f"{worker_id}_prep_a")
    await scheduler.yield_point(f"{worker_id}_prep_b")
    scheduler.stamp(
        DraftEvent(
            type=EventType.STATE_WRITE,
            payload={
                "key": _PIPELINE_KEY,
                "value_hash": value_hash,
                "prev_value_hash": None,
                "reducer": None,
                "txn_id": None,
                "lock_id": None,
            },
            agent_id=worker_id,
            clock_slot=worker_id,
            span_id=f"span_{worker_id}",
        )
    )


async def _pipeline_root(scheduler: Scheduler) -> None:
    """Spawn two concurrent, unsynchronised writers to the same pipeline key."""
    scheduler.spawn(_pipeline_writer(scheduler, "stage_a", "vh:pipeline-a"), agent_id="stage_a")
    scheduler.spawn(_pipeline_writer(scheduler, "stage_b", "vh:pipeline-b"), agent_id="stage_b")


def _run(
    *, run_id: str, seed: int, root_factory: _RootFactory, delay_schedule: DelaySchedule
) -> tuple[Event, ...]:
    """Build a fresh `Scheduler`, run `root_factory(scheduler)` to completion, return its log."""
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(run_id, sink, batch_size=1)
    config = SchedulerConfig(strict_determinism=True, step_budget=100_000)
    scheduler = Scheduler(
        run_id=run_id,
        seed=seed,
        clock=clock,
        writer=writer,
        config=config,
        policy="random",
        delay_schedule=dict(delay_schedule),
    )
    asyncio.run(scheduler.run(root_factory(scheduler)))
    return sink.events()


def make_research_fanout_executor(*, seed: int = RESEARCH_FANOUT_SEED) -> ScheduleExecutor:
    """Return a `ScheduleExecutor` for the `research_fanout`-shaped scenario at `seed`."""

    def execute(delay_schedule: DelaySchedule) -> tuple[Event, ...]:
        return _run(
            run_id=RESEARCH_FANOUT_RUN_ID,
            seed=seed,
            root_factory=_fanout_root,
            delay_schedule=delay_schedule,
        )

    return execute


def make_code_pipeline_executor(*, seed: int = CODE_PIPELINE_SEED) -> ScheduleExecutor:
    """Return a `ScheduleExecutor` for the `code_pipeline`-shaped scenario at `seed`."""

    def execute(delay_schedule: DelaySchedule) -> tuple[Event, ...]:
        return _run(
            run_id=CODE_PIPELINE_RUN_ID,
            seed=seed,
            root_factory=_pipeline_root,
            delay_schedule=delay_schedule,
        )

    return execute


__all__ = [
    "CODE_PIPELINE_SEED",
    "RESEARCH_FANOUT_SEED",
    "ScheduleExecutor",
    "make_code_pipeline_executor",
    "make_research_fanout_executor",
]
