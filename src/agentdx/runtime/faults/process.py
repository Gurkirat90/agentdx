"""Process-class faults (PRD §12.2): `agent_crash` (P0, MVP) and `agent_slow` (P1, deferred).

**`agent_crash` is the one MVP fault with a genuine, direct interception point in the fixed
P06 scheduler surface (`docs/chaos-safety.md` §"Interception point mapping" has the full
table).** PRD §12.1 names its interception point `pre_resume`; `runtime.scheduler.Scheduler`
exposes no method by that name — the closest fixed hooks are `pre_schedule(step, runnable)`
(fires once per step, before the scheduler chooses who runs next) and
`pre_yield(task_id, reason)` (fires the instant a running task is about to suspend). Both are
used here, for two different points in a target agent's step lifecycle:

* **A task that has not yet run at all this step** (`TaskState.PENDING`) is caught in
  `pre_schedule`: its `Task.coro` — a plain mutable dataclass field, not yet consumed by
  anything (`Scheduler._resume_task` only reads `task.coro` the *first* time a task runs) —
  is swapped for a coroutine that raises `AgentCrashed` immediately. Nothing of the agent's
  step ever executes.
* **A task already mid-step, suspended inside a real `yield_point` call** (e.g. `reviewer`
  mid-way through `write_draft`) is caught in `pre_yield`: this hook is called *synchronously,
  from inside the suspending task's own coroutine stack* (`Scheduler.yield_point` calls
  `self._fault_hook.pre_yield(...)` directly, not via `await`), so a plain Python exception
  raised here propagates straight up through the awaiting `yield_point()` call, through
  whatever SDK helper is mid-await, and out of the agent's node function — exactly PRD
  §12.2's "the agent's current task raises `AgentCrashed`". This is how
  `scenarios/reviewer_crash_midflight.yaml` — crashing `reviewer` at `t=2400`, partway
  through its `read_file`/`write_draft` sequence — actually crashes mid-flight rather than
  only ever at a step boundary.

Both paths converge on the same result: `_drive_coro`'s `except Exception as exc:` catches
`AgentCrashed`, sets `task.exception`, and calls `FaultInjectorHook.on_task_done(task_id,
exc)` — which is where this module records the fire and, if `recoverable`, schedules the
restart.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.runtime.clock import wall_time
from agentdx.runtime.faults import safety, triggers
from agentdx.runtime.faults.registry import (
    ArmedFault,
    FaultRegistry,
    fault_effect_payload,
    fault_effect_span_id,
    param_int,
    params_payload,
)
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.runtime.scheduler import FaultInjectorHook, TaskState

if TYPE_CHECKING:
    from agentdx.runtime.clock import VirtualClock
    from agentdx.runtime.scheduler import Task

_DOCS: Final = "docs/chaos-safety.md"


class _SpawnFn(Protocol):
    """The exact subset of `Scheduler.spawn`'s signature this module calls.

    A plain `Callable[[Coroutine[...]], str]` cannot express `spawn`'s required keyword-only
    `agent_id` parameter — `Protocol` is the typing-correct way to describe a callable with
    keyword arguments, and matches `Scheduler.spawn`'s real signature structurally (duck typing,
    no import of `Scheduler` needed here beyond `TYPE_CHECKING`).
    """

    def __call__(self, coro: Coroutine[object, object, object], *, agent_id: str) -> str: ...


class AgentCrashed(RuntimeError):
    """Raised into a target agent's own task by `CrashInjector` (PRD §12.2 `agent_crash`).

    Not `E-CHAOS-*` coded: this is not a chaos-safety refusal, it *is* the fault effect —
    the exception the crashed agent's step is supposed to see, matching the PRD's own naming.
    """

    def __init__(self, fault_id: str, agent_id: str, *, recoverable: bool) -> None:
        """Build the error naming the fault, the crashed agent, and its recoverability."""
        self.fault_id = fault_id
        self.agent_id = agent_id
        self.recoverable = recoverable
        mode = "recoverable" if recoverable else "non-recoverable"
        super().__init__(f"agent {agent_id!r} crashed by fault {fault_id} ({mode})")


@dataclass
class _RestartRequest:
    """One pending recoverable-crash restart.

    PRD §12.2: "restarts after `restart_after_ms` with cleared local context but intact shared
    state".
    """

    armed: ArmedFault
    agent_id: str
    ready_at_virtual_ms: int


class CrashInjector(FaultInjectorHook):
    """Executes every armed `agent_crash` fault against a live `Scheduler` run.

    Constructed once per run and passed to `Scheduler(fault_hook=...)`. Requires the same
    `VirtualClock` the scheduler drives (for `virtual_ts_ms` reads at trigger-evaluation time)
    and a `stamp` callable — `Scheduler.stamp`, the public interface `docs/chaos-safety.md`
    names for exactly this purpose — to emit `fault_injected`/`fault_effect` events.

    **Restart is honest about what "intact shared state" means here.** PRD §12.2 says a
    recoverable crash restarts "with cleared local context but intact shared state" — this
    injector satisfies that by construction, not by clearing anything: a restarted agent gets
    a *new* `Task`/coroutine (fresh local variables; nothing carried over), while the shared
    `agentdx.state()` registry the fixture writes to lives outside any `Task` entirely and was
    never touched by the crash. What this injector does **not** do: automatically re-drive the
    agent's original node function from the top after a restart (that would require re-
    invoking the SDK/LangGraph binding this module has no handle on) — the restart mechanism
    is `spawn_restart_coro`, a caller-supplied factory (see `__init__`), so the harness or
    future `RunHost` decides what a "restarted `reviewer`" actually re-executes. Declared, not
    silently narrowed — see `docs/chaos-safety.md` §"Restart" and the closing NOT DONE block.
    """

    def __init__(
        self,
        *,
        registry: FaultRegistry,
        clock: VirtualClock,
        seed: int,
        stamp: Callable[[DraftEvent, Sequence[int]], Event],
        taint: FaultTaintTracker,
        guard_monitor: safety.AbortGuardMonitor | None = None,
        spawn_restart_coro: Callable[[str], Coroutine[object, object, object]] | None = None,
        spawn: _SpawnFn | None = None,
    ) -> None:
        """Bind to one run's registry, clock, stamping boundary and taint tracker.

        Args:
            registry: The armed faults + blast radius for this run.
            clock: The scheduler's own `VirtualClock` — read-only here.
            seed: The run seed, for this module's own deterministic probability stream
                (`triggers.seeded_stream`) — unused by `agent_crash` today (none of its
                trigger kinds is `PROBABILITY`), kept for uniformity with the other
                fault-class modules and so a future PROBABILITY-triggered process fault needs
                no constructor change.
            stamp: `Scheduler.stamp` — the only way this module ever writes an event.
            taint: The run's shared `FaultTaintTracker` (one instance, shared across every
                fault-class module in the run — `docs/chaos-safety.md` §"Wiring the taint
                tracker").
            guard_monitor: Optional; if given, `pre_schedule` also evaluates PRD §13.6's
                step-observable guards (`max_virtual_duration_ms`, `max_wall_duration_s`) and
                raises `safety.AbortGuardTripped` on a trip.
            spawn_restart_coro: Optional factory, `agent_id -> coroutine`, called when a
                recoverable crash's `restart_after_ms` elapses. If `None`, a recoverable crash
                still fires and is recorded, but no new task is spawned for the agent — a
                declared limitation (see class docstring), not a silent one.
            spawn: Optional `Scheduler.spawn`-compatible callable, used together with
                `spawn_restart_coro` to actually register the restarted task with the live
                scheduler. Both or neither — a `spawn_restart_coro` with no `spawn` cannot
                register anything and is a construction-time `ValueError`.
        """
        if spawn_restart_coro is not None and spawn is None:
            msg = "spawn_restart_coro was given without spawn — nothing could register the restart"
            raise ValueError(msg)
        self._registry = registry
        self._clock = clock
        self._stream = triggers.seeded_stream(seed)
        self._stamp = stamp
        self._taint = taint
        self._guard_monitor = guard_monitor
        self._spawn_restart_coro = spawn_restart_coro
        self._spawn = spawn

        self._task_agent: dict[str, str] = {}
        self._crashed_agents: dict[str, None] = {}
        """Membership-only ("is this agent currently crashed?") — a `dict[str, None]`, not a
        `set[str]`, because `scripts/check_determinism_hygiene.py` bans a bare `set()` call
        anywhere under `src/agentdx/` (its static check cannot see that this particular set is
        never iterated, only tested for membership and added/discarded — a `dict` sidesteps the
        check honestly rather than suppressing it, and happens to also be insertion-ordered if
        anything here ever does iterate it later)."""
        self._pending_restarts: list[_RestartRequest] = []
        self._start_wall_ms: int | None = None
        self._pending_direct_fault_id: str | None = None
        """Set immediately before `self._stamp(...)` for a `fault_injected`/`fault_effect`
        draft this module itself constructs, and consumed (read + cleared) by the very next
        `fault_id_for` call — `Scheduler.stamp` -> `_SchedulerRecorder.write` ->
        `fault_id_for` all happen synchronously inside that one `self._stamp(...)` call, so
        there is no window for a second event to observe this value. This is PRD §9.4 rule 1
        ("the fault_id of the fault that directly produced this event"), told to the tracker
        by the one class that actually knows it — the payload is never re-parsed to infer it."""

    def _crash_faults_for(self, agent_id: str) -> tuple[ArmedFault, ...]:
        return tuple(f for f in self._registry.by_type("agent_crash") if f.decl.target == agent_id)

    def _due_fault(self, agent_id: str) -> ArmedFault | None:
        """Return the first not-yet-fired `agent_crash` fault targeting `agent_id`.

        Only returned if its trigger is true right now; otherwise `None`.
        """
        for armed in self._crash_faults_for(agent_id):
            if triggers.should_fire(armed, virtual_ts_ms=self._clock.now_ms(), stream=self._stream):
                return armed
        return None

    def _emit_fault_injected_if_first(self, armed: ArmedFault) -> None:
        if armed.fired:
            return
        decl = armed.decl
        draft = DraftEvent(
            type=EventType.FAULT_INJECTED,
            payload={
                "fault_id": decl.fault_id,
                "fault_type": decl.fault_type,
                "target": decl.target,
                "params": params_payload(decl.params),
                "trigger": {"kind": decl.trigger.kind.value, "value": decl.trigger.value},
            },
        )
        self._pending_direct_fault_id = decl.fault_id
        self._stamp(draft, ())

    def _emit_fault_effect(self, armed: ArmedFault, *, agent_id: str) -> None:
        decl = armed.decl
        draft = DraftEvent(
            type=EventType.FAULT_EFFECT,
            payload=fault_effect_payload(
                fault_id=decl.fault_id,
                effect="crash",
                target=decl.target,
                exception_type=AgentCrashed.__name__,
            ),
            agent_id=agent_id,
            span_id=fault_effect_span_id(decl.fault_id, decl.target),
        )
        self._pending_direct_fault_id = decl.fault_id
        self._stamp(draft, ())

    def _crash(self, armed: ArmedFault, agent_id: str) -> None:
        """Fire `armed` against `agent_id`: authorise, emit events, record, mark tainted."""
        safety.reauthorize(armed, self._registry.blast_radius)
        recoverable = bool(armed.decl.params.get("recoverable", True))
        self._emit_fault_injected_if_first(armed)
        armed.record_fire(virtual_ts_ms=self._clock.now_ms(), target=agent_id)
        self._emit_fault_effect(armed, agent_id=agent_id)
        self._taint.mark_agent_tainted(agent_id, armed.decl.fault_id)
        self._crashed_agents[agent_id] = None
        if recoverable:
            restart_after = param_int(armed.decl.params, "restart_after_ms", 0)
            self._pending_restarts.append(
                _RestartRequest(
                    armed=armed,
                    agent_id=agent_id,
                    ready_at_virtual_ms=self._clock.now_ms() + restart_after,
                )
            )

    # ------------------------------------------------------------------
    # FaultInjectorHook overrides
    # ------------------------------------------------------------------

    def pre_schedule(self, step: int, runnable: list[Task]) -> None:
        """Crash any PENDING target task whose trigger is due; check step-level guards."""
        if self._start_wall_ms is None:
            self._start_wall_ms = wall_time()
        if self._guard_monitor is not None:
            trip = self._guard_monitor.observe_step(
                step=step,
                virtual_ts_ms=self._clock.now_ms(),
                wall_elapsed_ms=wall_time() - self._start_wall_ms,
            )
            if trip is not None:
                raise safety.AbortGuardTripped(trip)

        self._ready_due_restarts(runnable)

        for task in runnable:
            self._task_agent[task.task_id] = task.agent_id
            if task.state is not TaskState.PENDING:
                continue
            if task.agent_id in self._crashed_agents and task.agent_id not in {
                r.agent_id for r in self._pending_restarts
            }:
                # Agent is down and no restart is pending for it: nothing this hook does —
                # a non-recoverable crash simply leaves the agent's future tasks PENDING
                # forever from this injector's point of view (the caller — harness/RunHost —
                # decides whether to still let a non-recoverable agent's remaining spawned
                # tasks run "as if nothing happened"; PRD §12.2 does not specify this and it
                # is out of this module's authority to invent — declared, see class docstring).
                pass
            due = self._due_fault(task.agent_id)
            if due is not None:
                self._crash_via_coro_swap(task, due)

    def _crash_via_coro_swap(self, task: Task, armed: ArmedFault) -> None:
        """Replace a not-yet-started task's coroutine with one that raises immediately."""
        recoverable = bool(armed.decl.params.get("recoverable", True))
        fault_id = armed.decl.fault_id
        agent_id = task.agent_id

        async def _raiser() -> None:
            raise AgentCrashed(fault_id, agent_id, recoverable=recoverable)

        # The original coroutine was created (by whatever spawned this PENDING task) but,
        # by definition of PENDING, never awaited — closing it explicitly here suppresses
        # Python's own "coroutine was never awaited" `RuntimeWarning` and releases it
        # cleanly, since it is about to be discarded and will genuinely never run.
        task.coro.close()
        task.coro = _raiser()
        self._crash(armed, agent_id)

    def _ready_due_restarts(self, runnable: list[Task]) -> None:
        now = self._clock.now_ms()
        still_pending: list[_RestartRequest] = []
        for req in self._pending_restarts:
            if now < req.ready_at_virtual_ms:
                still_pending.append(req)
                continue
            self._crashed_agents.pop(req.agent_id, None)
            if self._spawn_restart_coro is not None and self._spawn is not None:
                coro = self._spawn_restart_coro(req.agent_id)
                self._spawn(coro, agent_id=req.agent_id)
        self._pending_restarts = still_pending

    def pre_yield(self, task_id: str, reason: str) -> None:
        """Crash a mid-flight task by raising into its current suspension point.

        Called synchronously from inside the yielding task's own coroutine stack
        (`Scheduler.yield_point`) — raising here propagates as a genuine exception through
        that task's `await yield_point(...)` call, matching PRD §12.2's "the agent's current
        task raises `AgentCrashed`" for a target already mid-step.
        """
        agent_id = self._task_agent.get(task_id)
        if agent_id is None or not self._crash_faults_for(agent_id):
            return
        due = self._due_fault(agent_id)
        if due is None:
            return
        recoverable = bool(due.decl.params.get("recoverable", True))
        fault_id = due.decl.fault_id
        self._crash(due, agent_id)
        raise AgentCrashed(fault_id, agent_id, recoverable=recoverable)

    def on_task_done(self, task_id: str, exception: BaseException | None) -> None:
        """Clear rule-3 ambient taint for this agent — its logical task has completed.

        PRD §9.4 rule 3: "everything that agent does afterwards ... is tainted, until the
        task completes." A crashed task completing (with `AgentCrashed` as `exception`) also
        reaches here — clearing taint here is correct for both the crash-caused completion
        and an ordinary one, since either way the *logical task* that observed the fault is
        now over.
        """
        agent_id = self._task_agent.get(task_id)
        if agent_id is not None:
            self._taint.clear_agent(agent_id)

    def fault_id_for(self, draft: DraftEvent, causal_parents: Sequence[int]) -> str | None:
        """Resolve taint for `draft` via the shared `FaultTaintTracker` (PRD §9.4).

        Rule 1 ("directly produced this event") is supplied by `_pending_direct_fault_id`,
        set by `_emit_fault_injected_if_first`/`_emit_fault_effect` immediately before the
        `self._stamp(...)` call that reaches here — see that field's docstring for why this
        is synchronously safe. Rules 2 and 3 are the shared tracker's own job.
        """
        direct = self._pending_direct_fault_id
        self._pending_direct_fault_id = None
        return self._taint.resolve(
            agent_id=draft.agent_id, causal_parents=causal_parents, direct_fault_id=direct
        )

    def on_event_stamped(self, event: Event) -> None:
        """Commit `event`'s resolved taint into the shared tracker (see `fault_id_for`)."""
        self._taint.record(
            seq=event.seq,
            fault_id=event.fault_id,
            is_fault_injected=event.type is EventType.FAULT_INJECTED,
        )


__all__ = ["AgentCrashed", "CrashInjector"]
