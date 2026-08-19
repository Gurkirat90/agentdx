"""Crash-and-retry cascade demo (Definition of Done's week-4 milestone).

`reviewer` crashes recoverably at `t=0` (`restart_after_ms=200`); `CrashInjector`'s
`spawn_restart_coro`/`spawn` wiring (see `process.CrashInjector`'s own class docstring, "Restart
is honest about what 'intact shared state' means here") respawns it as a genuinely new `Task`
once the virtual clock reaches `t=200`, and the restarted agent runs to completion.

**A real finding surfaces here, not just a happy-path demo.** PRD §9.4 rule 3 ("the fault_id
carried on the agent's context after it observed a faulted input ... until the task completes")
is implemented by `FaultTaintTracker.mark_agent_tainted`/`clear_agent`, and `process.
CrashInjector._crash` calls `mark_agent_tainted` for the crashing agent. But `agent_crash`
always ends its own task via an immediately-raised exception — `Scheduler._drive_coro`'s
`except Exception` clause calls `on_task_done` (which calls `clear_agent`) synchronously, in
the same call stack, with no `await` in between. So `mark_agent_tainted` is set and cleared
back-to-back, before any other code — in particular, before the *restarted* task, a genuinely
new `Task`/`agent_id` pairing spawned later — ever gets a chance to observe it.
`test_a_naively_restarted_agents_events_are_not_tainted_by_rule_3` below proves this
concretely: it is not a hypothesis, it is measured behaviour of this exact build.

This does not mean a restart's events *can't* carry the crash's taint — it means rule 3 is not
the mechanism that does it here. `test_a_restart_wired_with_an_explicit_causal_edge_is_tainted`
shows the mechanism that does work: the restart coroutine (caller-supplied, per `CrashInjector`'s
own docstring — this module has no opinion on what a restarted agent does) declares an explicit
`causes=` edge back to the `fault_effect` event on its first stamped action, which is rule 2,
not rule 3 — and is exactly as legitimate a way to say "this happened because of the crash" as
any other declared causal edge. See `docs/chaos-safety.md` §"Restart and rule 3" for the write-up
and the closing NOT DONE block for why this is recorded as a gap rather than silently patched.
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

RUN_ID = "r_crash_retry_cascade"
RESTART_AFTER_MS = 200


def _build(*, wire_causal_edge: bool) -> tuple[Scheduler, MemorySink]:
    resolved = resolved_scenario(
        faults=[
            {
                "type": "agent_crash",
                "agent": "reviewer",
                "at_virtual_ts": 0,
                "recoverable": True,
                "restart_after_ms": RESTART_AFTER_MS,
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

    def _spawn_restart(agent_id: str) -> object:
        async def _restarted() -> None:
            """A fresh task, fresh local state — PRD §12.2's "cleared local context"."""
            await scheduler.yield_point("restarted_step0")
            causes: tuple[int, ...] = ()
            if wire_causal_edge:
                fault_effect = next(e for e in sink.events() if e.type is EventType.FAULT_EFFECT)
                causes = (fault_effect.seq,)
            scheduler.stamp(
                DraftEvent(
                    type=EventType.TOOL_CALL,
                    payload=sample_payload(EventType.TOOL_CALL, salt=7),
                    agent_id=agent_id,
                    span_id="span_reviewer_restarted",
                ),
                causes=causes,
            )

        return _restarted()

    def _spawn(coro: object, *, agent_id: str) -> str:
        return scheduler.spawn(coro, agent_id=agent_id)  # type: ignore[arg-type]

    injector = CrashInjector(
        registry=registry,
        clock=clock,
        seed=1,
        stamp=_stamp,
        taint=taint,
        spawn_restart_coro=_spawn_restart,  # type: ignore[arg-type]
        spawn=_spawn,
    )
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
    """Crashed via coro-swap before it ever runs (`at_virtual_ts: 0`)."""


async def _monitor(scheduler: Scheduler) -> None:
    """Keeps the virtual clock moving past `RESTART_AFTER_MS`.

    `CrashInjector._ready_due_restarts` only fires a pending restart when the scheduler's own
    virtual clock reaches `ready_at_virtual_ms` — and the clock only advances because *some*
    task is sleeping toward a future timestamp. With no other agent in this fixed scenario,
    nothing would ever wake the scheduler up to notice the restart is due; a real `RunHost`
    would have other agents doing this incidentally. Declared here rather than silently
    special-cased.
    """
    await scheduler.yield_point("monitor_step0")
    await scheduler.sleep(RESTART_AFTER_MS + 50)
    await scheduler.yield_point("monitor_step1")


def _run(*, wire_causal_edge: bool) -> tuple[Event, ...]:
    scheduler, sink = _build(wire_causal_edge=wire_causal_edge)

    async def _root() -> None:
        scheduler.spawn(_pending_reviewer(), agent_id="reviewer")
        scheduler.spawn(_monitor(scheduler), agent_id="monitor")

    asyncio.run(scheduler.run(_root()))
    return sink.events()


def test_recoverable_crash_actually_restarts_the_agent_as_a_new_task() -> None:
    """The cascade, mechanically: crash -> `restart_after_ms` elapses -> new task runs."""
    events = _run(wire_causal_edge=False)
    print("\ncrash+retry cascade:")  # noqa: T201
    for event in events:
        print(  # noqa: T201
            f"  seq={event.seq} type={event.type.value} agent={event.agent_id} "
            f"fault_id={event.fault_id}"
        )

    fault_effect = next(e for e in events if e.type is EventType.FAULT_EFFECT)
    assert fault_effect.payload["target"] == "reviewer"
    assert fault_effect.payload["effect"] == "crash"

    restarted_events = [e for e in events if e.agent_id == "reviewer" and e.seq > fault_effect.seq]
    assert len(restarted_events) == 1  # the restarted task really did run and stamp once
    assert restarted_events[0].virtual_ts_ms >= RESTART_AFTER_MS


def test_a_naively_restarted_agents_events_are_not_tainted_by_rule_3() -> None:
    """Measured, not hypothesised — see module docstring for the mechanism.

    Rule 3's `mark_agent_tainted` call has no observable effect here, because the crashing
    task's own completion clears it first.
    """
    events = _run(wire_causal_edge=False)
    fault_effect = next(e for e in events if e.type is EventType.FAULT_EFFECT)
    restarted_event = next(
        e for e in events if e.agent_id == "reviewer" and e.seq > fault_effect.seq
    )
    assert restarted_event.fault_id is None


def test_a_restart_wired_with_an_explicit_causal_edge_is_tainted() -> None:
    """The mechanism that *does* work — the caller's own decision, not automatic.

    The restart coroutine declares its own `causes=` edge back to the crash's `fault_effect`
    event, which is rule 2.
    """
    events = _run(wire_causal_edge=True)
    fault_effect = next(e for e in events if e.type is EventType.FAULT_EFFECT)
    restarted_event = next(
        e for e in events if e.agent_id == "reviewer" and e.seq > fault_effect.seq
    )
    assert restarted_event.fault_id == "f_00"
    assert list(restarted_event.causal_parents) == [fault_effect.seq]
