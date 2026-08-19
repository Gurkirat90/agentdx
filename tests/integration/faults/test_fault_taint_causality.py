"""Dedicated proof of Definition of Done's fault-taint requirement, end to end.

"A fault-taint test proving exactly causally-downstream events carry `fault_id` and a
concurrent-unrelated branch does not." Gate G4 already demonstrates this incidentally
(`test_gate_g4.py`), but this file is the direct, minimal proof: a real `Scheduler` +
`CrashInjector` run in which one branch is wired with genuine, explicit multi-hop `causes=`
edges downstream of a fault, and a second, concurrent branch shares nothing but `seq` adjacency
with it.

**This test exists because of a real bug this exact scenario caught.** An earlier version of
`_SchedulerRecorder.write` (`runtime.scheduler`) fed the fault-taint hook `Scheduler.
_causal_parents`'s own fallback-inclusive output — which synthesises a `[seq-1]` linear parent
for *any* event stamped with no explicit `causes` (every `schedule_decision`, and any bystander
event stamped without a declared edge). That made a concurrent, causally-unrelated agent's own
event inherit taint purely by landing next in `seq` order after a fault — see `runtime.scheduler.
_SchedulerRecorder.write`'s "Deliberately NOT `causal`" comment and `runtime.faults.taint`'s
module docstring for the fix. `test_a_concurrent_branch_with_no_causal_edge_is_never_tainted`
below is the regression test for exactly that bug.
"""

from __future__ import annotations

import asyncio

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

RUN_ID = "r_taint_causality"


def _build() -> tuple[Scheduler, MemorySink]:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 0}]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(RUN_ID, sink, batch_size=1)
    taint = FaultTaintTracker()

    def _stamp(draft: DraftEvent, causes: object) -> Event:
        return scheduler.stamp(draft, causes)  # type: ignore[arg-type]

    injector = CrashInjector(registry=registry, clock=clock, seed=1, stamp=_stamp, taint=taint)
    config = SchedulerConfig(strict_determinism=True, step_budget=100_000)
    scheduler = Scheduler(
        run_id=RUN_ID,
        seed=1,
        clock=clock,
        writer=writer,
        config=config,
        policy="random",
        fault_hook=injector,
    )
    return scheduler, sink


async def _pending_reviewer() -> None:
    """Crashed via coro-swap before it ever runs (`at_virtual_ts: 0`, PENDING at step 0)."""


async def _downstream_hop_1(scheduler: Scheduler, fault_effect_seq: int) -> int:
    """An event whose *declared* `causes` names the fault_effect event directly — rule 2, hop 1."""
    event = scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=1),
            agent_id="supervisor",
            span_id="span_supervisor",
        ),
        causes=(fault_effect_seq,),
    )
    return event.seq


async def _downstream_hop_2(scheduler: Scheduler, hop1_seq: int) -> int:
    """An event caused by hop 1, not by the fault directly — rule 2 must still chain through."""
    event = scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=2),
            agent_id="supervisor",
            span_id="span_supervisor",
        ),
        causes=(hop1_seq,),
    )
    return event.seq


async def _concurrent_bystander(scheduler: Scheduler) -> None:
    """A different agent's own event, stamped with no declared `causes` at all.

    Runs interleaved with (and, depending on scheduler ordering, may land immediately after)
    the fault's own events in `seq` order — the exact adjacency the fixed bug used to
    mistake for causation.
    """
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=3),
            agent_id="tester",
            span_id="span_tester",
        )
    )


def test_causally_downstream_events_are_tainted_and_the_concurrent_branch_is_not() -> None:
    scheduler, sink = _build()

    async def _root() -> None:
        scheduler.spawn(_pending_reviewer(), agent_id="reviewer")

        async def _chain() -> None:
            await scheduler.yield_point("wait_for_fault")
            events = sink.events()
            fault_effect = next(e for e in events if e.type is EventType.FAULT_EFFECT)
            hop1_seq = await _downstream_hop_1(scheduler, fault_effect.seq)
            await _downstream_hop_2(scheduler, hop1_seq)

        scheduler.spawn(_chain(), agent_id="supervisor")
        scheduler.spawn(_concurrent_bystander(scheduler), agent_id="tester")

    asyncio.run(scheduler.run(_root()))

    events = {e.seq: e for e in sink.events()}
    fault_effect = next(e for e in events.values() if e.type is EventType.FAULT_EFFECT)
    fault_injected = next(e for e in events.values() if e.type is EventType.FAULT_INJECTED)
    hop1 = next(
        e
        for e in events.values()
        if e.agent_id == "supervisor" and list(e.causal_parents) == [fault_effect.seq]
    )
    hop2 = next(
        e
        for e in events.values()
        if e.agent_id == "supervisor" and list(e.causal_parents) == [hop1.seq]
    )
    bystander = next(e for e in events.values() if e.agent_id == "tester")

    # Rule 1: the fault's own two events.
    assert fault_injected.fault_id == "f_00"
    assert fault_effect.fault_id == "f_00"
    # Rule 2, two hops deep: genuinely declared `causes` edges inherit taint transitively.
    assert hop1.fault_id == "f_00"
    assert hop2.fault_id == "f_00"
    # The regression this test exists for: no declared causal edge to the fault at all.
    assert bystander.fault_id is None
    assert list(bystander.causal_parents) != [fault_effect.seq]


def test_a_concurrent_branch_with_no_causal_edge_is_never_tainted_even_when_seq_adjacent() -> None:
    """Narrower isolation of the regression.

    Force the bystander to be the *very next* `seq` after the fault's own events by not
    spawning any other task, then assert it stays clean.
    """
    scheduler, sink = _build()

    async def _root() -> None:
        scheduler.spawn(_pending_reviewer(), agent_id="reviewer")

        async def _bystander_immediately_after() -> None:
            await scheduler.yield_point("after_crash")
            scheduler.stamp(
                DraftEvent(
                    type=EventType.TOOL_CALL,
                    payload=sample_payload(EventType.TOOL_CALL, salt=9),
                    agent_id="tester",
                    span_id="span_tester",
                )
            )

        scheduler.spawn(_bystander_immediately_after(), agent_id="tester")

    asyncio.run(scheduler.run(_root()))

    events = sink.events()
    fault_effect_seq = next(e.seq for e in events if e.type is EventType.FAULT_EFFECT)
    bystander = next(e for e in events if e.agent_id == "tester")

    # It really is seq-adjacent (or close to it) — proving this isn't tainted because it's far
    # away in the log, but because it declared no causal edge.
    assert bystander.seq > fault_effect_seq
    assert bystander.fault_id is None
