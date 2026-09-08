"""A `code_pipeline`-shaped, Scheduler-driven scenario wired for real §34.6 fault scoring.

Same posture as `tests/integration/faults/_harness.py` (fake tasks, no LLM/graph/fixture,
real `Scheduler`/`CrashInjector`/`FaultRegistry` — mission Design Constraint 6) but extended
in one load-bearing way that harness does not need for its own gate-G4 purpose: this module's
`_tester` stamps a genuine `success_check` `assertion_result` event, computed from whether
`coder` and `reviewer` actually delivered their messages before `tester` finishes — the exact
input `agentdx.analysis.resilience.score` requires (`_success_check_events`/
`_last_success_check_passed`) and that gate-G4's own harness never produces (it always logs
one `TOOL_CALL` regardless of outcome — correct for *that* harness's own narrower "cascade
shape" purpose, PRD §19 aware but out of P09's own scope).

**Why a new scenario, not `tests.integration.faults._harness` reused directly.** That module
is shared, already-`VERIFIED`-adjacent test infrastructure for gate G4 specifically (`CascadeShape`,
not a `resilience.score`-ready log) — changing its `_tester` to add a success/failure signal
would be a behavior change to a gate's own regression harness, exactly the kind of scope creep
`AGENTS.md` §2 warns against. This module borrows its *shape* (the same `planner -> {coder,
reviewer} -> tester` topology, matching `fixtures/code_pipeline/graph.py`) and its *pattern*
(a real `Scheduler` + `CrashInjector` + `FaultRegistry`, no LLM/graph/fixture) without touching
the file G4 depends on.

**Why only `agent_crash`, stated once here rather than per call site.** `cli/host.py`'s own
module docstring (verified, not merely cited): "`runtime.scheduler.Scheduler(fault_hook=...)`
accepts exactly one hook, and only `runtime.faults.process.CrashInjector` (`agent_crash`) is
built as a `FaultInjectorHook` at all — `transport.TransportFaultInjector` (`latency`,
`message_drop`) and `dependency.DependencyFaultInjector` (`tool_failure`) are... 'pure decision
logic... never registered as `Scheduler(fault_hook=...)`, meant to be consulted from a live SDK
call site that does not exist yet.'" This is a pre-existing, already-disclosed, project-wide
gap (also `docs/chaos-safety.md`), not something this benchmark discovers — `agent_crash` is
the only one of the four MVP fault types this codebase can fire through a live `Scheduler` run
today, and `bench/harness/resilience_scoring.py` discloses this prominently rather than
fabricating results for the other three.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from tests.unit.events.factories import sample_payload
from tests.unit.faults.conftest import resolved_scenario
from tests.unit.runtime.conftest import MemorySink

import agentdx
from agentdx.config import SchedulerConfig
from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock, wall_time
from agentdx.runtime.faults.process import CrashInjector
from agentdx.runtime.faults.registry import FaultRegistry
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.scheduler import Scheduler

RUN_ID = "r_p184resilience"

#: Matches `tests/integration/faults/_harness.py`'s own `kill_reviewer.yaml`-derived timing so
#: the crash lands mid-flight, not before the target has done any real work.
REVIEWER_CRASH_AT_VIRTUAL_MS = 3000
CODER_CRASH_AT_VIRTUAL_MS = 150


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """One real run's own event log, plus the crash target that was armed (or `None`)."""

    crash_target: str | None
    seed: int
    events: tuple[Event, ...]


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


async def _coder(scheduler: Scheduler, received: list[str]) -> None:
    await scheduler.yield_point("coder_step0")
    await scheduler.yield_point("coder_step1")
    await scheduler.sleep(CODER_CRASH_AT_VIRTUAL_MS)
    # `Scheduler.sleep()` itself never calls `fault_hook.pre_yield` (verified by direct read of
    # `runtime/scheduler.py::sleep` — it only sets BLOCKED/registers a timer/awaits a Future; the
    # hook is invoked from `yield_point` alone, line ~697). The explicit `yield_point` below is
    # therefore load-bearing, not decorative — it's the actual crash-check call, matching the
    # proven-working `tests/integration/faults/_harness.py::_reviewer` pattern
    # (`sleep(...)` then `yield_point(...)`). `CrashInjector.pre_yield` raises `AgentCrashed`
    # synchronously from inside this call when this agent is the armed target — nothing below
    # runs if so.
    await scheduler.yield_point("coder_step2")
    received.append("coder")
    scheduler.stamp(
        DraftEvent(
            type=EventType.MESSAGE_SEND,
            payload=sample_payload(EventType.MESSAGE_SEND, salt=2),
            agent_id="coder",
            clock_slot="coder",
            span_id="span_coder",
        )
    )


async def _reviewer(scheduler: Scheduler, received: list[str]) -> None:
    await scheduler.yield_point("reviewer_step0")
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=3),
            agent_id="reviewer",
            span_id="span_reviewer",
        )
    )
    await scheduler.sleep(REVIEWER_CRASH_AT_VIRTUAL_MS)
    # See `_coder`'s comment: the crash check happens in `yield_point`, not `sleep` itself.
    await scheduler.yield_point("reviewer_step1")
    # Crash point for a "reviewer" target, same timing G4's own scenario uses.
    received.append("reviewer")
    scheduler.stamp(
        DraftEvent(
            type=EventType.MESSAGE_SEND,
            payload=sample_payload(EventType.MESSAGE_SEND, salt=4),
            agent_id="reviewer",
            clock_slot="reviewer",
            span_id="span_reviewer",
        )
    )


async def _tester(scheduler: Scheduler, received: list[str]) -> None:
    """Waits past every possible crash point, then stamps a real `success_check` result.

    `passed` is computed from `received` — a plain, single-threaded list both `_coder` and
    `_reviewer` append to immediately before their own `message_send` stamp, so its contents
    at the point `_tester` reads it are exactly "which upstream agents actually delivered,"
    the same shared-mutable-state coordination pattern `tests/integration/faults/_harness.py`
    already uses for its own `marker` list — not a new technique, an established one.
    """
    await scheduler.yield_point("tester_step0")
    await scheduler.sleep(REVIEWER_CRASH_AT_VIRTUAL_MS + 500)
    scheduler.stamp(
        DraftEvent(
            type=EventType.TOOL_CALL,
            payload=sample_payload(EventType.TOOL_CALL, salt=5),
            agent_id="tester",
            span_id="span_tester",
        )
    )
    passed = "coder" in received and "reviewer" in received
    # ASSERTION_RESULT is RUN-scoped (events/schema.py's EVENT_SCOPES), not SPAN-scoped like
    # the TOOL_CALL above — no agent_id/span_id, matching run_start/run_end and
    # tests.analysis._events.assertion_result's own builder (which never sets either).
    scheduler.stamp(
        DraftEvent(
            type=EventType.ASSERTION_RESULT,
            payload={
                "assertion_id": "pipeline_success_check",
                "kind": "success_check",
                "passed": passed,
                "expected": "coder and reviewer both delivered their message",
                "actual": f"received={sorted(received)}",
            },
        )
    )


def _build_registry(crash_target: str | None) -> FaultRegistry:
    if crash_target is None:
        return FaultRegistry.from_resolved_scenario(
            resolved_scenario(faults=[]), is_fixture_target=True
        )
    at_virtual_ts = (
        REVIEWER_CRASH_AT_VIRTUAL_MS if crash_target == "reviewer" else CODER_CRASH_AT_VIRTUAL_MS
    )
    resolved = resolved_scenario(
        faults=[
            {
                "type": "agent_crash",
                "agent": crash_target,
                "at_virtual_ts": at_virtual_ts,
                "recoverable": False,
            }
        ]
    )
    return FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)


async def _run_async(*, seed: int, crash_target: str | None) -> tuple[Event, ...]:
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(RUN_ID, sink, batch_size=1)
    taint = FaultTaintTracker()
    registry = _build_registry(crash_target)

    scheduler_box: list[Scheduler] = []

    def _stamp(draft: DraftEvent, causes: object) -> Event:
        return scheduler_box[0].stamp(draft, causes)  # type: ignore[arg-type]

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
    scheduler_box.append(scheduler)

    received: list[str] = []

    async def _root() -> None:
        scheduler.spawn(_planner(scheduler), agent_id="planner")
        scheduler.spawn(_coder(scheduler, received), agent_id="coder")
        scheduler.spawn(_reviewer(scheduler, received), agent_id="reviewer")
        scheduler.spawn(_tester(scheduler, received), agent_id="tester")

    # `Scheduler.run` itself never stamps `run_start`/`run_end` — that is the *run host*'s job
    # (its own docstring: "on successful completion... writing run_start/run_end... are the run
    # host's job", quoted verbatim in `cli/host.py::CliRunHost`'s class docstring). This module
    # has no `RunHost`/`Store` (I3 purity — no `runtime.cache`/`sdk` dependency needed for a
    # bare Scheduler scenario), so it stamps a minimal, honestly-synthetic pair itself, mirroring
    # `CliRunHost.open_run`/`close_run`'s own field shape — `resilience.score()` requires both
    # (`_run_end`/`classify_degradation` read `run_end.status`; `E-RES-001` fires without one).
    start_wall_ms = wall_time()
    started_at = (
        datetime.fromtimestamp(start_wall_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    )
    scheduler.stamp(
        DraftEvent(
            type=EventType.RUN_START,
            payload={
                "seed": seed,
                "mode": "chaos" if crash_target is not None else "baseline",
                "cache_mode": "passthrough",
                "scenario_id": None,
                "scenario_hash": "bench-harness-p184-resilience",
                "graph_hash": "bench-harness-p184-resilience",
                "delay_schedule_hash": "no-delay-schedule",
                "calibration_id": None,
                "agentdx_version": agentdx.__version__,
                "sdk_version": agentdx.__version__,
                "model": "n/a",
                "provider_host": "n/a",
                "provider_sdk_version": f"bench-harness/{agentdx.__version__}",
                "host": "bench-harness",
                "pid": os.getpid(),
                "started_at_utc": started_at,
                "env": {},
            },
        )
    )

    await scheduler.run(_root())

    # `status` is always "complete" here — `Scheduler.run(_root())` above returned without
    # raising for every `crash_target` tested (`None`/`'reviewer'`/`'coder'`, live-verified),
    # because a non-recoverable `agent_crash` (`recoverable=False`, see `_build_registry`)
    # terminates only the crashed agent's own task (`CrashInjector`'s own docstring: "a non-
    # recoverable crash simply leaves the agent's future tasks PENDING forever... the caller...
    # decides whether to still let [it]... run") — it does not fail the root coroutine `_tester`
    # runs under. This is not a benchmark artifact: a crash that terminates silently, with the
    # run itself still reporting "complete", is exactly PRD §19.5's `SILENT_FAILURE` shape when
    # `success_check` also fails — the scenario is honestly measuring that case, not papering
    # over a "should have been failed" run.
    events_before_end = sink.events()
    scheduler.stamp(
        DraftEvent(
            type=EventType.RUN_END,
            payload={
                "status": "complete",
                "virtual_makespan_ms": clock.virtual_ms(),
                "wall_makespan_ms": wall_time() - start_wall_ms,
                "event_count": len(events_before_end) + 1,
                "total_llm_calls": 0,
                "total_tool_calls": sum(
                    1 for e in events_before_end if e.type is EventType.TOOL_CALL
                ),
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
            },
        )
    )
    return sink.events()


def run_scenario(*, seed: int, crash_target: str | None) -> ScenarioOutcome:
    """Run the pipeline once, synchronously; `crash_target` is `None` for the no-fault baseline."""
    events = asyncio.run(_run_async(seed=seed, crash_target=crash_target))
    return ScenarioOutcome(crash_target=crash_target, seed=seed, events=events)
