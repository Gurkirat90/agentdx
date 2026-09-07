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

import pytest

from agentdx.config import SchedulerConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.faults.process import AgentCrashed, CrashInjector
from agentdx.runtime.faults.registry import BlastRadius, FaultRegistry
from agentdx.runtime.faults.safety import (
    AbortGuardMonitor,
    AbortGuardTripped,
    ChaosAuthorizationError,
)
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
        # allow_total_failure: true — most callers here spawn "reviewer" as the harness's
        # only real agent (root's own "root" agent_id does not count as live; see
        # CrashInjector._known_live_agents), so without this the PRD §12.2 Safety row this
        # class now enforces (op2-audit-p09-second.md finding #1) would silently skip the
        # crash these tests exist to exercise. The dedicated single-agent Safety tests below
        # construct their own fault dicts explicitly, without this override, specifically to
        # exercise the refusal.
        else [
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 3000,
                "allow_total_failure": True,
            }
        ]
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
        seed=1,
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 0,
                "allow_total_failure": True,
            }
        ],
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


async def _solo_target(marker: list[str]) -> None:
    """A single-agent run's only agent — never yields, so nothing else ever runs alongside it."""
    marker.append("ran")


def test_single_agent_crash_is_silently_skipped_without_allow_total_failure() -> None:
    """PRD §12.2 Safety: "cannot crash the last live agent unless `allow_total_failure: true`".

    op2-audit-p09-second.md finding #1 (CRITICAL): a single-agent run crashed its only agent
    unconditionally before this fix — no notion of "how many agents are still live" existed
    anywhere in this file. A trigger that is due but blocked by this rule is silently skipped
    (not disarmed — see `CrashInjector._due_fault`'s own docstring), so the agent's own
    coroutine runs to completion undisturbed and no `fault_injected`/`fault_effect` event is
    ever written for it.
    """
    scheduler, sink, _injector = _build(
        seed=1, faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 0}]
    )
    marker: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_solo_target(marker), agent_id="reviewer")

    asyncio.run(scheduler.run(_root()))

    assert marker == ["ran"]  # the agent's own body ran to completion — never crashed
    fault_events = [
        e for e in sink.events() if e.type in (EventType.FAULT_INJECTED, EventType.FAULT_EFFECT)
    ]
    assert fault_events == []


def test_single_agent_crash_with_allow_total_failure_false_is_also_skipped() -> None:
    """Explicit `allow_total_failure: false` behaves identically to leaving it unset."""
    scheduler, sink, _injector = _build(
        seed=1,
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 0,
                "allow_total_failure": False,
            }
        ],
    )
    marker: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_solo_target(marker), agent_id="reviewer")

    asyncio.run(scheduler.run(_root()))

    assert marker == ["ran"]
    fault_events = [
        e for e in sink.events() if e.type in (EventType.FAULT_INJECTED, EventType.FAULT_EFFECT)
    ]
    assert fault_events == []


def test_single_agent_crash_with_allow_total_failure_true_fires_normally() -> None:
    """`allow_total_failure: true` is the explicit opt-in that lifts the Safety row's refusal."""
    scheduler, sink, _injector = _build(
        seed=1,
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 0,
                "allow_total_failure": True,
            }
        ],
    )
    marker: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_solo_target(marker), agent_id="reviewer")

    asyncio.run(scheduler.run(_root()))

    assert marker == []  # crashed before its own body ever ran
    fault_events = [
        e for e in sink.events() if e.type in (EventType.FAULT_INJECTED, EventType.FAULT_EFFECT)
    ]
    assert [e.type for e in fault_events] == [EventType.FAULT_INJECTED, EventType.FAULT_EFFECT]


def test_multi_agent_crash_still_fires_when_a_survivor_remains() -> None:
    """Sanity check: the Safety row must not over-trigger — a crash with a survivor still fires."""
    scheduler, sink, _injector = _build(
        seed=1, faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 0}]
    )
    marker: list[str] = []

    async def _bystander() -> None:
        await scheduler.yield_point("bystander_step")

    async def _root() -> None:
        scheduler.spawn(_pending_target(marker), agent_id="reviewer")
        scheduler.spawn(_bystander(), agent_id="coder")

    asyncio.run(scheduler.run(_root()))

    assert marker == []  # reviewer's own body never ran — crashed before first run
    fault_events = [
        e for e in sink.events() if e.type in (EventType.FAULT_INJECTED, EventType.FAULT_EFFECT)
    ]
    assert len(fault_events) == 2


def test_fire_time_reauthorization_refuses_a_crash_whose_radius_narrowed_after_arming() -> None:
    """PRD §13.4's runtime defence-in-depth check, exercised through a real `CrashInjector`.

    op2-audit-p09-second.md finding #3: every real fire-time reauthorization test in this
    suite called `safety.reauthorize` directly, never through a real injector encountering a
    blast radius narrowed after arming — a mutation deleting all four production
    `safety.reauthorize(...)` call sites left the entire suite green. This test arms inside
    the radius, narrows the *live registry's* `blast_radius` (the same object `_crash` re-reads
    on every call — see `_crash`'s own `self._registry.blast_radius` read), then fires, and
    requires `ChaosAuthorizationError` — deleting `process.py`'s `safety.reauthorize` call must
    turn this test red.
    """
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 0}],
        chaos_opt_in=True,
        blast_radius={"agents": ["reviewer"]},
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    clock = VirtualClock()
    taint = FaultTaintTracker()

    def _stamp(draft: DraftEvent, causes: object) -> None:
        return None

    injector = CrashInjector(registry=registry, clock=clock, seed=1, stamp=_stamp, taint=taint)  # type: ignore[arg-type]

    # Narrow the radius after arming — "reviewer" is no longer authorised, though it was when
    # `FaultRegistry.from_resolved_scenario` armed it above. `FaultRegistry` is a plain
    # (non-frozen) dataclass; only `blast_radius` itself (`BlastRadius`) is frozen.
    registry.blast_radius = BlastRadius(agents=frozenset({"coder"}))
    armed = registry.faults[0]

    with pytest.raises(ChaosAuthorizationError) as excinfo:
        injector._crash(armed, "reviewer")
    assert "E-CHAOS-001" in str(excinfo.value)


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
