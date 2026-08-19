"""Unit tests for `runtime.faults.process` — `CrashInjector` against a real `Scheduler`.

Every test here drives the actual `Scheduler` (fake tasks, no LLM/graph/fixture — same
posture as `tests/determinism/_harness.py`), so a schema-incorrect `fault_injected`/
`fault_effect` payload fails loudly through the real `validate_event` call inside
`_SchedulerRecorder.write`, not just through a hand-rolled assertion.

**Wiring order.** `CrashInjector.__init__` needs `stamp=scheduler.stamp`, but `Scheduler.
__init__` takes `fault_hook=` as a constructor argument — a genuine circular-construction
fact about how P09 attaches to the fixed P06 `Scheduler`, not a test-only workaround. The
fix used here (and the pattern any real caller must also use) is a `stamp` closure that
captures the `scheduler` name by reference and resolves it lazily, at call time — by then
`scheduler` is always bound, since nothing calls `stamp` before `Scheduler.run()` starts.
"""

from __future__ import annotations

import asyncio

from agentdx.config import SchedulerConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.faults.process import AgentCrashed, CrashInjector
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.safety import AbortGuardMonitor, AbortGuardTripped
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.scheduler import Scheduler
from tests.unit.events.factories import sample_payload
from tests.unit.faults.conftest import resolved_scenario
from tests.unit.runtime.conftest import MemorySink

RUN_ID = "r_faults_process"


def _build(
    *,
    seed: int = 42,
    faults: list[dict[str, object]] | None = None,
    guard_monitor: AbortGuardMonitor | None = None,
) -> tuple[Scheduler, MemorySink, CrashInjector]:
    resolved = resolved_scenario(
        faults=faults
        if faults is not None
        else [{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 3000}]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(RUN_ID, sink, batch_size=1)
    taint = FaultTaintTracker()

    def _stamp(draft: DraftEvent, causes: object) -> Event:
        return scheduler.stamp(draft, causes)  # type: ignore[arg-type]

    injector = CrashInjector(
        registry=registry,
        clock=clock,
        seed=seed,
        stamp=_stamp,
        taint=taint,
        guard_monitor=guard_monitor,
    )
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
    return scheduler, sink, injector


async def _pending_target(marker: list[str]) -> None:
    """`reviewer`'s coroutine when the crash is due before it ever runs — must never execute."""
    marker.append("ran")


async def _mid_flight_target(scheduler: Scheduler, marker: list[str]) -> None:
    """`reviewer`'s coroutine — yields once immediately, sleeps to t=3000, yields again."""
    await scheduler.yield_point("step0")
    marker.append("step0_done")
    await scheduler.sleep(3000)
    await scheduler.yield_point("step1")  # crash fires here, mid-flight, via pre_yield
    marker.append("step1_done")  # must never execute


def test_crash_via_coro_swap_before_first_run_never_executes_agent_code() -> None:
    scheduler, sink, _injector = _build(
        seed=1, faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 0}]
    )
    marker: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_pending_target(marker), agent_id="reviewer")

    asyncio.run(scheduler.run(_root()))

    assert marker == []  # the agent's own coroutine body never ran
    events = sink.events()
    fault_events = [
        e for e in events if e.type in (EventType.FAULT_INJECTED, EventType.FAULT_EFFECT)
    ]
    assert [e.type for e in fault_events] == [EventType.FAULT_INJECTED, EventType.FAULT_EFFECT]
    injected, effect = fault_events
    assert injected.payload["fault_id"] == "f_00"
    assert effect.payload["fault_id"] == "f_00"
    assert effect.payload["effect"] == "crash"
    assert effect.payload["target"] == "reviewer"


def test_crash_mid_flight_raises_into_the_agents_own_yield_point() -> None:
    scheduler, sink, _injector = _build(seed=7)
    marker: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_mid_flight_target(scheduler, marker), agent_id="reviewer")

    asyncio.run(scheduler.run(_root()))

    assert marker == ["step0_done"]  # step1_done never appended — crashed before it ran
    events = sink.events()
    fault_effects = [e for e in events if e.type is EventType.FAULT_EFFECT]
    assert len(fault_effects) == 1
    assert fault_effects[0].payload["exception_type"] == AgentCrashed.__name__


def test_fault_effect_event_carries_its_own_fault_id_as_taint() -> None:
    scheduler, sink, _injector = _build(seed=7)
    marker: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_mid_flight_target(scheduler, marker), agent_id="reviewer")

    asyncio.run(scheduler.run(_root()))

    fault_effects = [e for e in sink.events() if e.type is EventType.FAULT_EFFECT]
    assert fault_effects[0].fault_id == "f_00"  # rule 1: directly produced by the fault


def test_a_concurrent_unrelated_agents_events_are_never_tainted() -> None:
    scheduler, sink, _injector = _build(seed=7)
    marker: list[str] = []

    async def _bystander() -> None:
        await scheduler.yield_point("bystander_step")
        scheduler.stamp(
            DraftEvent(
                type=EventType.TOOL_CALL,
                payload=sample_payload(EventType.TOOL_CALL),
                agent_id="tester",
                span_id="span_tester",
            )
        )

    async def _root() -> None:
        scheduler.spawn(_mid_flight_target(scheduler, marker), agent_id="reviewer")
        scheduler.spawn(_bystander(), agent_id="tester")

    asyncio.run(scheduler.run(_root()))

    events = sink.events()
    bystander_events = [e for e in events if e.agent_id == "tester"]
    assert bystander_events  # the bystander did run and produce an event of its own
    assert all(e.fault_id is None for e in bystander_events)


def test_recoverable_crash_records_a_pending_restart() -> None:
    resolved = resolved_scenario(
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 1000,
                "recoverable": True,
                "restart_after_ms": 500,
            }
        ]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    clock = VirtualClock()
    taint = FaultTaintTracker()
    calls: list[DraftEvent] = []

    def _stamp(draft: DraftEvent, causes: object) -> None:
        calls.append(draft)

    injector = CrashInjector(
        registry=registry,
        clock=clock,
        seed=1,
        stamp=_stamp,
        taint=taint,  # type: ignore[arg-type]
    )

    armed = registry.faults[0]
    injector._crash(armed, "reviewer")

    assert len(injector._pending_restarts) == 1
    assert injector._pending_restarts[0].ready_at_virtual_ms == 500
    assert "reviewer" in injector._crashed_agents
    assert len(calls) == 2  # fault_injected + fault_effect


def test_non_recoverable_crash_records_no_pending_restart() -> None:
    resolved = resolved_scenario(
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 1000,
                "recoverable": False,
            }
        ]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    clock = VirtualClock()
    taint = FaultTaintTracker()

    def _stamp(draft: DraftEvent, causes: object) -> None:
        return None

    injector = CrashInjector(
        registry=registry,
        clock=clock,
        seed=1,
        stamp=_stamp,
        taint=taint,  # type: ignore[arg-type]
    )

    armed = registry.faults[0]
    injector._crash(armed, "reviewer")

    assert injector._pending_restarts == []
    assert "reviewer" in injector._crashed_agents


def test_guard_monitor_wired_through_pre_schedule_trips_and_raises() -> None:
    monitor = AbortGuardMonitor(
        max_virtual_duration_ms=0,
        max_tokens=200_000,
        max_retries=20,
        max_wall_duration_s=300,
        max_events=500_000,
        max_llm_calls=500,
    )
    scheduler, _sink, _injector = _build(seed=1, guard_monitor=monitor)
    marker: list[str] = []

    async def _agent() -> None:
        await scheduler.sleep(1)  # advances virtual time past the 0ms budget
        await scheduler.yield_point("step")
        marker.append("ran")

    async def _root() -> None:
        scheduler.spawn(_agent(), agent_id="reviewer")

    raised = False
    try:
        asyncio.run(scheduler.run(_root()))
    except AbortGuardTripped as exc:
        raised = True
        assert exc.trip.guard == "max_virtual_duration_ms"
    assert raised
