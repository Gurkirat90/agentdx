"""Gate: the chaos safety architecture (PRD §13) actually refuses what it says it refuses.

Definition of Done asks for four end-to-end refusal demonstrations: an unauthorized target, a
missing blast radius, a missing/unmeasured hypothesis metric, and a tripped abort guard (with
the abort case's own output pasted into the report). Each test below drives the real
`FaultRegistry`/`safety` code paths — no mocking of the refusal logic itself — and the abort-
guard test runs a real `Scheduler` + `CrashInjector` to completion (or, here, non-completion).

**Two distinct `ChaosAuthorizationError` classes, deliberately.** `registry.py` and `safety.py`
each define their own `ChaosAuthorizationError` (same `E-CHAOS-001` code, different base class
and constructor) — PRD §13.4's "enforced at two layers" is two genuinely different checks: one
at *arm time* (`FaultRegistry.from_resolved_scenario`, `registry.ChaosAuthorizationError`,
structural — is this scenario even allowed to declare this fault at all) and one at *fire time*
(`safety.reauthorize`, `safety.ChaosAuthorizationError`, a defence-in-depth re-check every
fault-class module calls immediately before applying an effect). This module imports both,
aliased, and exercises each at its own layer.
"""

from __future__ import annotations

import asyncio

import pytest

from agentdx.config import SchedulerConfig
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock
from agentdx.runtime.faults.process import CrashInjector
from agentdx.runtime.faults.registry import (
    ArmedFault,
    BlastRadius,
    FaultDecl,
    FaultRegistry,
    Trigger,
)
from agentdx.runtime.faults.registry import ChaosAuthorizationError as ArmTimeAuthorizationError
from agentdx.runtime.faults.safety import (
    AbortGuardMonitor,
    AbortGuardTripped,
    AbortPrecondition,
    ChaosAuthorizationError,
    SteadyStateHypothesis,
    reauthorize,
)
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.scheduler import Scheduler
from agentdx.scenario.schema import TargetKind, TriggerKind
from tests.unit.faults.conftest import resolved_scenario
from tests.unit.runtime.conftest import MemorySink

RUN_ID = "r_safety_suite"


# ---------------------------------------------------------------------------------------
# 1. Unauthorized target — a fault whose declared target is outside the declared blast radius
# ---------------------------------------------------------------------------------------


def test_unauthorized_target_is_refused_at_arm_time() -> None:
    """A user-graph scenario opts in to chaos but scopes its blast radius to `coder` only.

    Declaring a fault against `reviewer` — outside that radius — must refuse to arm, not
    silently widen the radius or arm-and-hope the runtime re-check catches it later.
    """
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 1000}],
        chaos_opt_in=True,
        blast_radius={"agents": ["coder"]},  # reviewer is not in here
    )
    with pytest.raises(ArmTimeAuthorizationError) as excinfo:
        FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    message = str(excinfo.value)
    print(f"\nunauthorized-target refusal:\n  {message}")  # noqa: T201
    assert "E-CHAOS-001" in message
    assert "outside the resolved blast radius" in message


def test_unauthorized_target_is_also_refused_by_the_runtime_defence_in_depth_check() -> None:
    """`safety.reauthorize` — the second, runtime-side layer PRD §13.4 requires.

    Constructs an `ArmedFault` directly (bypassing `FaultRegistry.from_resolved_scenario`'s own
    arm-time gate) to exercise the re-check every fault-class module calls immediately before
    it applies an effect, independent of whether arm-time authorization ran at all.
    """
    decl = FaultDecl(
        fault_id="f_00",
        fault_type="agent_crash",
        target_kind=TargetKind.AGENT,
        target="reviewer",
        trigger=Trigger(kind=TriggerKind.AT_VIRTUAL_TS, value=1000),
        params={},
    )
    armed = ArmedFault(decl=decl)
    blast_radius = BlastRadius(agents=frozenset({"coder"}))
    with pytest.raises(ChaosAuthorizationError) as excinfo:
        reauthorize(armed, blast_radius)
    print(f"\nruntime defence-in-depth refusal:\n  {excinfo.value}")  # noqa: T201
    assert "E-CHAOS-001" in str(excinfo.value)


# ---------------------------------------------------------------------------------------
# 2. Missing blast radius (and/or missing chaos_opt_in) on a user-graph scenario
# ---------------------------------------------------------------------------------------


def test_user_graph_faults_with_no_blast_radius_declared_are_refused() -> None:
    """`chaos_opt_in: true` alone is not enough.

    PRD §13.3/§13.10 also requires a non-empty `blast_radius:`. No blast radius at all must
    refuse the same way an out-of-radius target does, not silently fall back to "universal"
    (that fallback is fixture-only, PRD §13.10).
    """
    resolved = resolved_scenario(
        faults=[{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1}],
        chaos_opt_in=True,
        blast_radius=None,  # declared nothing
    )
    with pytest.raises(ArmTimeAuthorizationError) as excinfo:
        FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)
    message = str(excinfo.value)
    print(f"\nmissing-blast-radius refusal:\n  {message}")  # noqa: T201
    assert "chaos_opt_in" in message
    assert "blast_radius" in message


def test_user_graph_faults_with_chaos_opt_in_false_are_also_refused() -> None:
    """The other half of the same gate: a declared blast radius with `chaos_opt_in` unset."""
    resolved = resolved_scenario(
        faults=[{"type": "tool_failure", "tool": "deploy", "always": True, "count": 1}],
        chaos_opt_in=False,
        blast_radius={"tools": ["deploy"]},
    )
    with pytest.raises(ArmTimeAuthorizationError):
        FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=False)


# ---------------------------------------------------------------------------------------
# 3. Missing / unmeasured steady-state hypothesis metric
# ---------------------------------------------------------------------------------------


def test_hypothesis_declares_a_metric_the_baseline_never_measured() -> None:
    """PRD §12.4: you cannot measure deviation from a steady state you never had.

    A `hypothesis:` section naming `task_success` is checked against baseline-phase metrics
    before any fault ever arms; if the baseline run never produced that metric, the precondition
    is unmeetable and the whole experiment aborts before a single `fault_injected` event exists.
    """
    resolved = resolved_scenario(hypothesis={"task_success": ">= 0.9"})
    hypothesis = SteadyStateHypothesis.from_resolved_scenario(resolved)
    baseline_metrics: dict[str, float] = {}  # the metric was never measured
    with pytest.raises(AbortPrecondition) as excinfo:
        hypothesis.check(baseline_metrics, phase="baseline")
    message = str(excinfo.value)
    print(f"\nmissing-hypothesis-metric refusal:\n  {message}")  # noqa: T201
    assert "task_success" in message
    assert "not measured" in message


# ---------------------------------------------------------------------------------------
# 4. Tripped abort guard — end to end, real Scheduler + CrashInjector
# ---------------------------------------------------------------------------------------


def _build_guarded_scheduler(monitor: AbortGuardMonitor) -> tuple[Scheduler, MemorySink]:
    resolved = resolved_scenario(
        faults=[{"type": "agent_crash", "agent": "reviewer", "at_virtual_ts": 5000}]
    )
    registry = FaultRegistry.from_resolved_scenario(resolved, is_fixture_target=True)
    clock = VirtualClock()
    sink = MemorySink()
    writer = EventWriter(RUN_ID, sink, batch_size=1)
    taint = FaultTaintTracker()

    def _stamp(draft: object, causes: object) -> object:
        return scheduler.stamp(draft, causes)  # type: ignore[arg-type]

    injector = CrashInjector(
        registry=registry,
        clock=clock,
        seed=1,
        stamp=_stamp,  # type: ignore[arg-type]
        taint=taint,
        guard_monitor=monitor,
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


def test_a_tripped_abort_guard_stops_the_run_and_the_partial_log_survives() -> None:
    """`max_virtual_duration_ms: 0` — any agent that ever sleeps trips it immediately.

    Demonstrates PRD §13.6's abort-guard architecture end to end: the guard is evaluated every
    scheduler step via `pre_schedule` (the one interception point this build wires live — see
    `AbortGuardMonitor`'s own docstring for the other three guards' declared wiring gap),
    raises `AbortGuardTripped`, and the run stops with whatever events were already written
    intact (NFR-13: "analysable partial log"), not silently swallowed or half-flushed.
    """
    monitor = AbortGuardMonitor(
        max_virtual_duration_ms=0,
        max_tokens=200_000,
        max_retries=20,
        max_wall_duration_s=300,
        max_events=500_000,
        max_llm_calls=500,
    )
    scheduler, sink = _build_guarded_scheduler(monitor)

    async def _agent() -> None:
        await scheduler.sleep(1)  # advances virtual time past the 0ms budget
        await scheduler.yield_point("step")

    async def _root() -> None:
        scheduler.spawn(_agent(), agent_id="reviewer")

    with pytest.raises(AbortGuardTripped) as excinfo:
        asyncio.run(scheduler.run(_root()))

    trip = excinfo.value.trip
    print(f"\ntripped-abort-guard output:\n  {excinfo.value}")  # noqa: T201
    print(f"partial log retained: {len(sink.events())} events written before the trip")  # noqa: T201

    assert trip.guard == "max_virtual_duration_ms"
    assert "E-GUARD-001" in str(excinfo.value)
    # The partial log is real, not empty — events up to the trip were flushed (NFR-13).
    assert len(sink.events()) > 0
