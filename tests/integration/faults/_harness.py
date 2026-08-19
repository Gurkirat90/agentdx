"""A fixed, `kill_reviewer.yaml`-shaped scenario for gate G4 and the faults-enabled G3 regression.

Same posture as `tests/determinism/_harness.py` (fake tasks, no LLM/graph/fixture — mission
Design Constraint 6) but shaped after `scenarios/kill_reviewer.yaml`: a `planner -> {coder,
reviewer} -> tester` pipeline (mirroring `fixtures/code_pipeline/graph.py`'s own topology)
with `reviewer` crashed mid-flight, non-recoverable, at `t=3000` — the exact fault that
scenario file declares. `agentdx scenario run scenarios/kill_reviewer.yaml --repeat 20` (the
gate's literal verification command) cannot run: `agentdx scenario run` is P17 (CLI, not yet
built) and no `RunHost` exists to wire a real graph through the scheduler (CONTEXT.md's own
open gap). This harness demonstrates the same claim — "killing reviewer at t=3000 reproduces
the same failure class and cascade shape, 20/20" — against the real `Scheduler` +
`CrashInjector`, the same way `tests/determinism/_harness.py` demonstrated gate G3 against a
hand-authored scenario rather than a real graph.

**"Cascade shape", defined here (not a PRD-given format).** PRD §12.2's own words for
`agent_crash`'s expected effect are "supervisor timeout/retry path exercised ... possible
cascade" — a resilience *classification* of that shape is P11's job (resilience scoring, out
of this prompt's scope, not yet built). `CascadeShape` below is this harness's own, minimal,
directly-observable stand-in: which agents' tasks ended in `AgentCrashed`, whether `tester`
took its fallback path (the observable cascade — `tester` depends causally on a review-
completion message from `reviewer` that never arrives once `reviewer` is dead), and the
sorted multiset of event types that ended up carrying the fault's `fault_id` (the taint
footprint). None of this is a resilience *score* — it is what gate G4 actually asks for:
proof that the same fault, at the same seed, produces the identical observable shape every
time.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass

from agentdx.config import SchedulerConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.faults.process import CrashInjector
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.scheduler import Scheduler
from tests.unit.events.factories import sample_payload
from tests.unit.faults.conftest import resolved_scenario
from tests.unit.runtime.conftest import MemorySink

RUN_ID = "r_g4demo"
SEED = 42  # matches scenarios/kill_reviewer.yaml's own `seed: 42`
CRASH_AT_VIRTUAL_MS = 3000  # matches scenarios/kill_reviewer.yaml's `at_virtual_ts: 3000`


@dataclass(frozen=True, slots=True)
class CascadeShape:
    """The observable "failure classification and cascade shape" gate G4 checks 20/20.

    See module docstring for what each field means and why it is defined here rather than
    quoting a PRD-given format (none exists for a P09-scope prompt).
    """

    crashed_agents: tuple[str, ...]
    tester_took_fallback_path: bool
    tainted_event_type_counts: tuple[tuple[str, int], ...]
    total_event_count: int


async def _planner(scheduler: Scheduler) -> None:
    await scheduler.yield_point("planner_step0")
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=1),
            agent_id="planner",
            span_id="span_planner",
        )
    )
    await scheduler.yield_point("planner_step1")


async def _coder(scheduler: Scheduler) -> None:
    await scheduler.yield_point("coder_step0")
    await scheduler.sleep(500)
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=2),
            agent_id="coder",
            span_id="span_coder",
        )
    )


async def _reviewer(scheduler: Scheduler) -> None:
    """Yields once immediately, sleeps to exactly t=3000, then yields again — crashed there."""
    await scheduler.yield_point("reviewer_step0")
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=3),
            agent_id="reviewer",
            span_id="span_reviewer",
        )
    )
    await scheduler.sleep(CRASH_AT_VIRTUAL_MS)
    # CrashInjector.pre_yield raises AgentCrashed synchronously from inside this call — none
    # of the code below it ever runs.
    await scheduler.yield_point("reviewer_step1")
    review_event = scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=4),
            agent_id="reviewer",
            span_id="span_reviewer",
        )
    )
    scheduler.stamp(
        DraftEvent(
            type=EventType.MESSAGE_SEND,
            payload=sample_payload(EventType.MESSAGE_SEND, salt=5),
            agent_id="reviewer",
            clock_slot="reviewer",
            span_id="span_reviewer",
        ),
        causes=(review_event.seq,),
    )


async def _tester(scheduler: Scheduler, marker: list[str]) -> None:
    """Waits past the crash point, then takes the fallback path.

    No review message ever arrives — the cascade `kill_reviewer.yaml`'s own PRD reference
    describes.
    """
    await scheduler.yield_point("tester_step0")
    await scheduler.sleep(CRASH_AT_VIRTUAL_MS + 500)
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=6),
            agent_id="tester",
            span_id="span_tester",
        )
    )
    marker.append("fallback")  # a real RunHost would branch on "review message present?"


def _build_registry() -> FaultRegistry:
    resolved = resolved_scenario(
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": CRASH_AT_VIRTUAL_MS,
                "recoverable": False,
            }
        ]
    )
    return FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)


def build_scheduler(seed: int = SEED) -> tuple[Scheduler, MemorySink]:
    """Build a fresh `Scheduler` wired to a fresh `CrashInjector`, ready to `run()`."""
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(RUN_ID, sink, batch_size=1)
    taint = FaultTaintTracker()
    registry = _build_registry()

    def _stamp(draft: DraftEvent, causes: object) -> Event:
        return scheduler.stamp(draft, causes)  # type: ignore[arg-type]

    injector = CrashInjector(registry=registry, clock=clock, seed=seed, stamp=_stamp, taint=taint)
    config = SchedulerConfig(strict_determinism=True, step_budget=100_000)
    scheduler = Scheduler(
        run_id=RUN_ID,
        seed=seed,
        clock=clock,
        writer=writer,
        config=config,
        policy="random",
        fault_hook=injector,
    )
    return scheduler, sink


async def _root(scheduler: Scheduler, marker: list[str]) -> None:
    scheduler.spawn(_planner(scheduler), agent_id="planner")
    scheduler.spawn(_coder(scheduler), agent_id="coder")
    scheduler.spawn(_reviewer(scheduler), agent_id="reviewer")
    scheduler.spawn(_tester(scheduler, marker), agent_id="tester")


async def run_scenario_async(seed: int = SEED) -> tuple[Event, ...]:
    """Run the fixed `kill_reviewer`-shaped scenario at `seed` to completion."""
    scheduler, sink = build_scheduler(seed)
    marker: list[str] = []
    await scheduler.run(_root(scheduler, marker))
    return sink.events()


def run_scenario(seed: int = SEED) -> tuple[Event, ...]:
    """Synchronous entry point (e.g. for a subprocess runner) with no running loop."""
    return asyncio.run(run_scenario_async(seed))


def cascade_shape(events: tuple[Event, ...]) -> CascadeShape:
    """Derive gate G4's "failure classification and cascade shape" from a completed run's log.

    Pure and deterministic: a function of `events` alone, in `seq` order.
    """
    crashed: set[str] = set()
    for event in events:
        if event.type is EventType.FAULT_EFFECT and event.payload.get("effect") == "crash":
            target = event.payload.get("target")
            if isinstance(target, str):
                crashed.add(target)

    tester_events = [e for e in events if e.agent_id == "tester"]
    took_fallback = len(tester_events) > 0  # this harness's tester always logs one TOOL_CALL

    tainted_counts = Counter(e.type.value for e in events if e.fault_id is not None)

    return CascadeShape(
        crashed_agents=tuple(sorted(crashed)),
        tester_took_fallback_path=took_fallback,
        tainted_event_type_counts=tuple(sorted(tainted_counts.items())),
        total_event_count=len(events),
    )
