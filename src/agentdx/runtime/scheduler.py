"""Cooperative scheduler, seeded choice, yield points, and the single stamping function.

PRD §10.1–10.2, §10.5–10.6, §10.9, §24.5, §6.4 lifecycle, §42.6 risk.

**Why this file is the whole product.**  The canonical projection (PRD §10.7) is byte-
identical across 100 replays only if every ``seq``, ``sched_step``, ``vclock`` and
``virtual_ts_ms`` is assigned in exactly one place, in exactly one order, as a pure
function of the seed and the task set.  This file is that one place.

**Design constraint 1 — single stamping point.**  ``_stamp_event`` is a private method
called from exactly one site: the top of ``_resume_task``, inside the scheduler's own
async generator / event loop.  There is no second path to ``Stamp``.  The ``stamp()``
method exposed to the SDK Recorder protocol calls the same internal function — it does not
build the ``Stamp`` itself — so the type checker enforces that nothing outside this class
can produce an ``Event``.

**Design constraint 2 — every sort is explicit.**  Search this file for any ``set``,
``dict``, or ``list`` iteration.  Every one is sorted by a stable total key before the
loop executes.  The self-audit confirms this.

**Design constraint 3 — leak detection is a runtime feature.**  The ``DeterminismGuard``
is installed for the full duration of ``run()``.  It redirects ``time.time()``,
``random.*``, ``uuid4``, ``datetime.now`` and catches thread spawning.
``asyncio.sleep`` is patched here (not in ``determinism.py``) because the redirect needs
a live reference to *this* scheduler instance, which ``determinism.py``'s module-level
install does not have.

**Out of scope (P09 and P07).**  The ``FaultInjectorHook`` and ``CacheHook`` protocols
are defined here as empty hook contracts so P09/P07 have a stable interface to implement;
their bodies are not built.

AGENTS.md §4.1 clauses:
- Clause 1: ``determinism.py`` — the seeded ``Random`` lives there.
- Clause 2: ``clock.py`` — owns virtual time.
- Clause 3: ``clock.wall_time()`` — the one real-clock call, on ``wall_ts_ms`` only.
- Clause 4: not this file (this file *is* the run context).
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from agentdx.config import SchedulerConfig
from agentdx.events.schema import (
    DraftEvent,
    Event,
    EventType,
    Stamp,
    VClock,
)
from agentdx.events.validators import validate_event
from agentdx.events.writer import EventWriter
from agentdx.runtime.clock import VirtualClock, wall_time
from agentdx.runtime.context import SchedTaskContext, use_task
from agentdx.runtime.determinism import (
    DeterminismGuard,
    LeakReport,
    NondeterminismLeakWarning,
    trap,
)

if TYPE_CHECKING:
    # Type-only: never called. A runtime `random.*` call outside determinism.py/clock.py
    # is what scripts/check_determinism_hygiene.py's BANNED_MODULES bans (AGENTS.md §4.1);
    # naming the class for an annotation, inside a block that never executes, is not one.
    from random import Random

_DOCS: Final = "docs/determinism-guarantees.md"
_T = TypeVar("_T")


# ---------------------------------------------------------------------------------------
# Errors (PRD §36)
# ---------------------------------------------------------------------------------------


class SchedulerError(RuntimeError):
    """A scheduler-internal invariant was violated.

    Carries ``E-SCHED-001`` (clock / internal bookkeeping broken) or ``E-SCHED-002``
    (illegal lifecycle transition).  Not raised for user-triggerable conditions.
    """

    code: Final = "E-SCHED-001"

    def __init__(self, detail: str, *, code: str = "E-SCHED-001") -> None:
        """Build the error from a detail string and an optional error code."""
        self.code = code  # type: ignore[misc]
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


class DeadlockError(SchedulerError):
    """No task is runnable and none is blocked on a timer — true deadlock.

    Carries ``E-SCHED-003`` (PRD §36: "No runnable task and no timer").  The wait-reasons
    map names every task and why it is stuck, so the message is evidence, not just a signal.
    """

    def __init__(self, wait_reasons: dict[str, str]) -> None:
        """Build the error from the map of task_id → wait_reason."""
        reasons = "; ".join(f"{t}: {r}" for t, r in sorted(wait_reasons.items()))
        super().__init__(
            f"deadlock: no task is runnable and none is blocked on a timer. "
            f"Wait reasons: {{{reasons}}}",
            code="E-SCHED-003",
        )


class LivelockError(SchedulerError):
    r"""The step budget was exhausted without a virtual-clock advance.

    Carries ``E-SCHED-003`` (same family as DeadlockError; PRD §36 shares the code for
    both \"no progress\" conditions).  ``SchedulerConfig.step_budget`` is the threshold.
    """

    def __init__(self, step_budget: int, sched_step: int) -> None:
        """Build the error naming both the budget and the step where it fired."""
        super().__init__(
            f"livelock: {sched_step} scheduler steps without a virtual-clock advance "
            f"(budget={step_budget}, E-SCHED-003). This usually means tasks yield "
            f"without consuming virtual time and will loop forever.",
            code="E-SCHED-003",
        )


class LifecycleTransitionError(SchedulerError):
    """An illegal run lifecycle transition was attempted.

    Carries ``E-SCHED-002`` (PRD §36: "Illegal lifecycle transition").
    """

    def __init__(self, current: RunState, requested: RunState) -> None:
        """Build the error naming both states."""
        super().__init__(
            f"illegal lifecycle transition: {current.value!r} → {requested.value!r}. "
            f"Valid transitions: CREATED→RUNNING, RUNNING→ANALYSING/FAILED/ABORTED_GUARD, "
            f"ANALYSING→COMPLETE/FAILED.",
            code="E-SCHED-002",
        )


# ---------------------------------------------------------------------------------------
# Lifecycle state machine (PRD §6.4)
# ---------------------------------------------------------------------------------------


class RunState(str, enum.Enum):  # noqa: UP042  # D-08: is `StrEnum` on a 3.12 toolchain
    """The six lifecycle states from PRD §6.4.

    Transitions are enforced by ``_transition``; an illegal move raises
    ``LifecycleTransitionError``.  ``COMPLETE`` and ``ABORTED_GUARD`` are terminal
    (append-only, per I2) — once reached, no further events may be written.
    """

    CREATED = "created"
    RUNNING = "running"
    ANALYSING = "analysing"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED_GUARD = "aborted_guard"


# Legal transitions as (from, to) pairs.  Any pair not in this set is illegal.
_LEGAL_TRANSITIONS: Final = frozenset(
    {
        (RunState.CREATED, RunState.RUNNING),
        (RunState.RUNNING, RunState.ANALYSING),
        (RunState.RUNNING, RunState.FAILED),
        (RunState.RUNNING, RunState.ABORTED_GUARD),
        (RunState.ANALYSING, RunState.COMPLETE),
        (RunState.ANALYSING, RunState.FAILED),
    }
)


# ---------------------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------------------


class TaskState(str, enum.Enum):  # noqa: UP042  # D-08: is `StrEnum` on a 3.12 toolchain
    """Internal task lifecycle — not exposed in the event log (that is RunState)."""

    PENDING = "pending"
    """Created but not yet started."""
    RUNNABLE = "runnable"
    """Ready to run; ``virtual_ready_ts_ms <= clock.now_ms()``."""
    RUNNING = "running"
    """Currently holds the scheduler baton."""
    BLOCKED = "blocked"
    """Waiting for a virtual-time event (timer) or an explicit unblock."""
    DONE = "done"
    """The coroutine returned or raised — permanently terminal."""


@dataclass
class Task:
    """One unit of cooperative scheduling (PRD §10.2).

    A ``Task`` wraps one coroutine — typically one agent step.  The scheduler drives it
    by calling ``asyncio.Future.set_result`` on ``resume_future`` each time it is chosen,
    which causes ``yield_point`` to return and the coroutine to proceed to its next yield.

    **Ordering key: ``(virtual_ready_ts_ms, agent_id, task_seq)``.**  All three components
    are integers or stable strings — no dict/set iteration, no object identity, no hash
    value leaks into this key.  The key is total: no two tasks share the same triple.
    """

    task_id: str
    agent_id: str
    task_seq: int  # monotone counter per agent
    coro: Coroutine[object, object, object]
    virtual_ready_ts_ms: int = 0
    state: TaskState = TaskState.PENDING
    wait_reason: str = ""
    result: object = None
    exception: BaseException | None = None

    def sort_key(self) -> tuple[int, str, int]:
        """Return the stable total key used by ``_collect_runnable``."""
        return (self.virtual_ready_ts_ms, self.agent_id, self.task_seq)


# ---------------------------------------------------------------------------------------
# P09 / P07 hook protocols — defined here so those prompts have a stable interface
# ---------------------------------------------------------------------------------------


class FaultInjectorHook:
    """Injection points P09 registers into the scheduler (out of scope until P09).

    Every method is a no-op by default so P06 ships without fault logic.
    P09 subclasses this and passes it to ``Scheduler.__init__``.
    """

    def pre_schedule(self, step: int, runnable: list[Task]) -> None:
        """Called after runnable tasks are collected, before ``choose``.

        Args:
            step: The current ``sched_step`` counter.
            runnable: The sorted list of runnable tasks (do not reorder).
        """

    def pre_yield(self, task_id: str, reason: str) -> None:
        """Called immediately before a task suspends at a yield point.

        Args:
            task_id: The task yielding.
            reason: The yield reason string the SDK passed.
        """

    def on_task_done(self, task_id: str, exception: BaseException | None) -> None:
        """Called when a task completes, successfully or with an error.

        Args:
            task_id: The task that finished.
            exception: The exception if it raised, else None.
        """

    def fault_id_for(self, draft: DraftEvent, causal_parents: Sequence[int]) -> str | None:
        """Return the PRD §9.4 taint marker this about-to-be-stamped event should carry.

        Added by P09 (`runtime/faults/`, `CONTEXT.md` D-43) — additive to the P06 hook
        contract, not a redefinition of it. Called from `_SchedulerRecorder.write`, once per
        event, after `causal_parents` is computed but before the `Stamp` is built; `Stamp.
        fault_id` (`events/schema.py`, present since P02, always `None` until this call site
        existed) is set to this method's return value. The default implementation always
        returns `None`, so a scheduler built with no fault hook (every P06/P07/P08 caller,
        unchanged) stamps every event exactly as before — this method is purely additive.

        Args:
            draft: The event about to be stamped. Not yet an `Event` — no `seq` exists yet.
            causal_parents: The *declared* causal parent seqs for this exact event — `sorted
                (set(causes))` if the caller passed explicit `causes` to `stamp`/`emit`, else
                empty. Deliberately **not** `Scheduler._causal_parents`'s output: that method
                additionally folds in a synthetic `[seq-1]` linear-chain fallback whenever
                `causes` is empty, purely to keep the hash-chain/vector-clock (PRD §9.3/§14.2)
                unbroken — a bookkeeping continuity guarantee, not a causation claim. PRD
                §9.4 rule 2 means genuinely-declared happens-after edges only; passing the
                fallback-inclusive value here would taint every `schedule_decision` (always
                empty `causes`) and any event that merely happens to follow a fault in `seq`
                order, which is what `Stamp.causal_parents` itself still records (unaffected
                by this distinction) but must not be what fault taint inherits through.

        Returns:
            The `fault_id` to stamp, or `None` for no taint. See
            `runtime.faults.taint.FaultTaintTracker.resolve`, whose signature this method
            exists to make callable from exactly this one site.
        """
        return None

    def on_event_stamped(self, event: Event) -> None:
        """Called once, immediately after `event` is successfully validated and written.

        Added alongside `fault_id_for` (CONTEXT.md D-43) as its commit-side counterpart: a
        fault-taint tracker that recorded state inside `fault_id_for` itself would record a
        seq that might never actually be written (a draft can still fail PRD §9.6 step-3
        validation after `fault_id_for` returns) — exactly the poisoning `_SchedulerRecorder.
        write`'s own "pure-compute-then-commit" comment already guards the vclock and seq
        counter against. This method is that same guarantee extended to fault-taint
        bookkeeping: called only after `self._writer.write(event)` has already succeeded.
        Default implementation does nothing.

        Args:
            event: The fully-stamped, already-persisted event.
        """
        return None


class CacheHook:
    """Injection point P07 registers to serve cached responses (out of scope until P07).

    Every method is a no-op placeholder; the real implementation lives in ``runtime/cache/``.
    """

    def on_llm_yield(self, task_id: str, cache_key: str) -> int | None:
        """Return the virtual duration for this LLM call if cached, else None.

        Args:
            task_id: Which task is making the call.
            cache_key: The canonical cache key for the LLM request.

        Returns:
            Virtual duration in milliseconds, or None if not cached / not in replay mode.
        """
        return None


# ---------------------------------------------------------------------------------------
# The Recorder — the single stamping boundary (design constraint 1)
# ---------------------------------------------------------------------------------------


class _SchedulerRecorder:
    """The ``sdk.generic.Recorder``-compatible stamping boundary, internal to the scheduler.

    This object is **the only place in the codebase that constructs a ``Stamp``**.
    ``EventWriter.write`` enforces that only a stamped ``Event`` can be persisted, so the
    only route from ``DraftEvent`` to persistence is: SDK builds draft → this object stamps
    it → ``EventWriter`` validates and persists it.

    **Thread-safety note:** ``EventWriter`` is explicitly not thread-safe (PRD §10.2:
    single OS thread), and so is this.  No lock is needed.
    """

    def __init__(
        self,
        *,
        run_id: str,
        writer: EventWriter,
        clock: VirtualClock,
        scheduler_ref: Scheduler,
    ) -> None:
        """Bind to one run, one writer, one clock, and the live scheduler.

        Args:
            run_id: The run every event belongs to.
            writer: The event writer that persists the event.
            clock: The virtual clock for ``virtual_ts_ms``.
            scheduler_ref: The live scheduler; provides ``sched_step`` and vclock.
        """
        self._run_id = run_id
        self._writer = writer
        self._clock = clock
        self._sched = scheduler_ref
        self._next_seq_val: int = 0

    # ------------------------------------------------------------------
    # sdk.generic.Recorder protocol
    # ------------------------------------------------------------------

    def write(self, draft: DraftEvent, causes: Sequence[int] = ()) -> Event:
        """Stamp ``draft``, validate, and persist via the writer.

        This is the **one call site** that constructs a ``Stamp``.  It is invoked:
        (a) via ``Scheduler.stamp``, for events built outside the scheduler loop — the run
        host's ``run_start``/``run_end`` (their payloads need the scenario, cache mode and
        analyser context this class does not have, so building them is the run host's job,
        not this class's; only the *stamping* happens here),
        (b) from ``Scheduler._emit_schedule_decision``/``_emit_nondeterminism_warning`` for
        the two event types the scheduler itself is the sole authority on, and
        (c) via ``emit``, from ``sdk.generic.emit`` — every SDK-side event (spans, messages,
        locks, LLM/tool calls) passes ``causes`` here, since only the caller that built the
        draft knows what it causally happens-after (PRD §9.3/§14.2).

        Every path goes through this method.  There is no second ``Stamp`` constructor.

        **Pure-compute-then-commit.**  ``seq`` and the vector clock are computed *before*
        anything is mutated, and every mutation of scheduler/recorder state — the seq
        counter, ``self._sched._vclocks``, ``self._sched._event_vclocks`` — happens only
        after both ``validate_event`` and ``self._writer.write`` have already succeeded.  A
        rejected draft therefore leaves the scheduler exactly as it was: the next call still
        sees the same ``seq`` and the same vclocks, so a validation failure can never poison
        the sequence counter or advance a slot's clock for an event that was never actually
        recorded.

        Args:
            draft: The unstamped event.
            causes: Seqs this event happens-after (PRD §9.3).  Folded into
                ``causal_parents`` and merged into the vector clock per PRD §14.2.  Empty
                for scheduler-internal events, which fall back to the linear chain.

        Raises:
            EventValidationError: the event failed PRD §9.6 step 3 validation.
            WriterStateError: the writer is sealed or the seq is wrong (I2).
            SchedulerError: ``causes`` names a seq this scheduler never recorded a vclock
                for (``E-SCHED-001``).
        """
        seq = self._next_seq_val
        slot = draft.clock_slot or draft.agent_id or "run"

        # Pure computation — nothing is mutated yet.
        tentative_vclock = self._sched._compute_vclock(slot, causes)
        causal = self._sched._causal_parents(seq, causes)
        # P09 addition (CONTEXT.md D-43): the fault-taint marker (PRD §9.4), resolved by
        # whatever FaultInjectorHook is installed. The default hook always returns None, so
        # this is a no-op for every caller that predates P09 (design constraint 1 is
        # unweakened: this is still the only place a Stamp is built).
        #
        # Deliberately NOT `causal`. `_causal_parents` folds in a synthetic `[seq-1]` linear
        # fallback whenever `causes` is empty — every `schedule_decision` (emitted every
        # single step) and any SDK/test event stamped with no declared edge — purely so the
        # hash-chain/vector-clock (PRD §9.3/§14.2) always has a provable predecessor; that
        # fallback asserts *log continuity*, not causation. PRD §9.4 rule 2 ("inherited from
        # causal_parents") means the caller-*declared* happens-after edges, not this
        # bookkeeping artefact — feeding `causal` in here would taint every scheduler-internal
        # event and any log-adjacent-but-unrelated event that follows a fault, which is
        # observably indistinguishable from a time window and violates the mission's own
        # "a concurrent unrelated branch does not [carry fault_id]" requirement (confirmed via
        # gate G4's harness: a bystander agent's own event, stamped with no `causes`, inherited
        # taint solely because it was next in `seq` order — see docs/chaos-safety.md §"Declared
        # vs. linear-fallback causal parents"). `causal` itself — fallback intact — is still
        # exactly what is written to `Stamp.causal_parents` below; this changes only what the
        # fault hook is shown, nothing about the persisted event, vclock or hash chain.
        declared_causal = sorted(set(causes)) if causes else []
        fault_id = self._sched._fault_hook.fault_id_for(draft, declared_causal)

        stamp = Stamp(
            seq=seq,
            sched_step=self._sched._step,
            virtual_ts_ms=self._clock.now_ms(),
            wall_ts_ms=wall_time(),
            vclock=tentative_vclock,
            causal_parents=causal,
            fault_id=fault_id,
        )
        event = Event.from_draft(draft, stamp, self._run_id)
        validate_event(event, self._writer._previous)
        self._writer.write(event)

        # Both checks above succeeded — now, and only now, commit state.
        self._next_seq_val = seq + 1
        self._sched._commit_vclock(slot, tentative_vclock)
        self._sched._event_vclocks[seq] = tentative_vclock
        # P09 addition (CONTEXT.md D-43): mirrors the vclock commit's own "only after success"
        # discipline — the fault hook's taint bookkeeping (if any) must not record a seq that
        # was never actually written, so it is told about the event only here, after
        # `self._writer.write` has already succeeded, never inside `fault_id_for` itself.
        self._sched._fault_hook.on_event_stamped(event)
        return event

    def emit(self, draft: DraftEvent, causes: Sequence[int]) -> int:
        """Stamp, validate and persist ``draft``; return its assigned ``seq``.

        The ``sdk.generic.Recorder`` protocol method — this is what ``sdk.generic.emit``
        calls for every SDK-authored event.  Delegates entirely to ``write``.
        """
        return self.write(draft, causes).seq


# ---------------------------------------------------------------------------------------
# The Scheduler
# ---------------------------------------------------------------------------------------


class Scheduler:
    """The cooperative, seeded, deterministic scheduler (PRD §10.2).

    Public interface (what the SDK sees via ``sdk.generic.Scheduler`` protocol):
    - ``await yield_point(reason)`` — suspend the current task.
    - ``await sleep(ms)`` — advance virtual clock by ``ms`` and block until then.

    Public interface (what the CLI/run-host calls):
    - ``await run(coro)`` — run a root coroutine to completion.
    - ``stamp(draft)`` — stamp a draft event (delegates to ``_SchedulerRecorder.write``).
    - ``state`` — the current ``RunState``.

    Everything else is internal.  Do not call private methods from outside this class.
    """

    def __init__(
        self,
        *,
        run_id: str,
        seed: int,
        clock: VirtualClock,
        writer: EventWriter,
        config: SchedulerConfig,
        policy: str = "random",
        delay_schedule: dict[int, int] | None = None,
        fault_hook: FaultInjectorHook | None = None,
        cache_hook: CacheHook | None = None,
    ) -> None:
        """Configure the scheduler.  Nothing is started until ``run()`` is called.

        Args:
            run_id: The run every event belongs to.
            seed: The sole source of scheduling non-determinism.
            clock: The shared ``VirtualClock`` this scheduler drives.
            writer: The ``EventWriter`` that persists events.
            config: ``SchedulerConfig`` from ``agentdx.toml`` — no literals here.
            policy: ``"random"`` (seeded) or ``"priority"`` (always pick first).
            delay_schedule: ``{sched_step: index}`` for exploration — P13.
            fault_hook: P09 injection points; no-op until P09.
            cache_hook: P07 cache coordination; no-op until P07.
        """
        self._run_id = run_id
        self._seed = seed
        self._clock = clock
        self._config = config
        self._policy = policy
        self._delay_schedule = delay_schedule or {}
        self._fault_hook = fault_hook or FaultInjectorHook()
        self._cache_hook = cache_hook or CacheHook()

        # Mutable scheduler state — everything here is modified only on the single OS thread.
        self._step: int = 0
        self._steps_without_clock_advance: int = 0
        # Per-slot vector clocks (PRD §14.2): each slot's own view, merged only across
        # explicit causal edges — never a single shared clock. Keyed by `clock_slot` or
        # `agent_id`.
        self._vclocks: dict[str, VClock] = {}
        # seq -> the vclock committed for that event, so a later `causes=(seq,)` can merge
        # it in. Never pruned at P06: PRD §14.2 requires any earlier seq to remain a valid
        # causal reference for the life of the run.
        self._event_vclocks: dict[int, VClock] = {}
        self._state: RunState = RunState.CREATED
        self._tasks: dict[str, Task] = {}
        self._task_seq_counter: dict[str, int] = {}
        self._timers: dict[str, int] = {}  # task_id → wake_at_virtual_ms

        # The guard and RNG are set in run(); None before start.
        self._guard: DeterminismGuard | None = None
        self._rng: Random | None = None  # set from guard.seeded_random

        # The recorder — the single stamping boundary.
        self._recorder = _SchedulerRecorder(
            run_id=run_id, writer=writer, clock=clock, scheduler_ref=self
        )

        # asyncio glue: each yield_point suspends on a Future the scheduler resolves.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task_futures: dict[str, asyncio.Future[None]] = {}
        # Captured in run(), before asyncio.sleep is patched to virtual. _resume_task
        # uses this — never the module-level asyncio.sleep — to yield to the real event
        # loop for one tick; the module-level name is the *patched* one for the duration
        # of a run, so awaiting it here would route a scheduler-internal dispatch yield
        # through Scheduler.sleep() as if the about-to-run task itself asked to block.
        self._real_asyncio_sleep: Callable[[float], Coroutine[object, object, None]] = asyncio.sleep

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> RunState:
        """Return the current lifecycle state."""
        return self._state

    @property
    def recorder(self) -> _SchedulerRecorder:
        """Return the stamping boundary (``sdk.generic.Recorder`` protocol)."""
        return self._recorder

    # ------------------------------------------------------------------
    # sdk.generic.Scheduler protocol
    # ------------------------------------------------------------------

    async def yield_point(self, reason: str) -> None:
        """Suspend the current task and give the scheduler control.

        The task resumes when the scheduler calls its resume future.  This is the only
        place a task can be preempted — it is the **entire** interleaving space.

        **This is preemption, not blocking.**  A yield point hands control back to the
        scheduler and re-enters the runnable pool *immediately* — ``virtual_ready_ts_ms``
        is unchanged, so the very next ``_collect_runnable()`` sees this task as a
        candidate again, exactly like every other runnable task.  Whether it is chosen
        again on the next step or another task runs first is ``choose()``'s decision, not
        this method's.  A yield point that actually needs to wait for virtual time to pass
        is ``sleep()``, which blocks on a timer instead — the two are deliberately
        different states (``RUNNABLE`` vs ``BLOCKED``) because only one of them is
        semantically "waiting for something".

        Args:
            reason: A human-readable label for the yield (``"llm_call"``, ``"state_read"``,
                etc.) — written into ``schedule_decision.reason``.

        Raises:
            SchedulerError: called from outside a scheduler-managed task (``E-SCHED-001``).
        """
        task_id = _current_task_id()
        task = self._tasks.get(task_id)
        if task is None:
            detail = (
                f"yield_point(reason={reason!r}) called from task {task_id!r} which is not "
                f"known to this scheduler — was this coroutine spawned outside ``run()``?"
            )
            raise SchedulerError(detail)
        self._fault_hook.pre_yield(task_id, reason)
        task.state = TaskState.RUNNABLE
        task.wait_reason = reason

        # Create a fresh Future for this yield; the scheduler resolves it when it resumes us.
        assert self._loop is not None  # noqa: S101 — always set inside run()
        fut: asyncio.Future[None] = self._loop.create_future()
        self._task_futures[task_id] = fut
        await fut  # suspend here until the scheduler calls fut.set_result(None)

    async def sleep(self, ms: int) -> None:
        """Block this task for ``ms`` virtual milliseconds (PRD §10.9).

        Does not block real wall time.  The virtual clock may or may not advance before
        this task is resumed — whether it does depends on whether other tasks are runnable
        at the time.

        Args:
            ms: Virtual sleep duration in milliseconds.  Must be non-negative.

        Raises:
            ValueError: ``ms`` is negative.
        """
        if ms < 0:
            msg = f"virtual sleep duration must be non-negative, got {ms}ms"
            raise ValueError(msg)
        task_id = _current_task_id()
        task = self._tasks.get(task_id)
        if task is None:
            detail = f"sleep({ms}ms) called from task {task_id!r} not known to this scheduler"
            raise SchedulerError(detail)
        wake_at = self._clock.now_ms() + ms
        task.state = TaskState.BLOCKED
        task.wait_reason = f"sleep({ms}ms)"
        task.virtual_ready_ts_ms = wake_at
        self._timers[task_id] = wake_at

        assert self._loop is not None  # noqa: S101
        fut: asyncio.Future[None] = self._loop.create_future()
        self._task_futures[task_id] = fut
        await fut

    # ------------------------------------------------------------------
    # Run entry point
    # ------------------------------------------------------------------

    async def run(self, root_coro: Coroutine[object, object, _T]) -> _T:
        """Execute ``root_coro`` to completion under deterministic cooperative scheduling.

        The full control flow from PRD §24.5::

            DeterminismGuard installed
            → asyncio.sleep patched to virtual
            → CREATED → RUNNING lifecycle transition
            → task loop: collect runnable → choose (seeded) → resume until yield
            → (fault hook pre_schedule, cache hook unused here)
            → clock advances when nothing runnable
            → loop ends when root task completes
            → RUNNING → ANALYSING (then COMPLETE by caller)
            DeterminismGuard uninstalled

        On successful completion the lifecycle moves to ``ANALYSING`` — writing the
        ``run_start``/``run_end`` events that bookend the log and driving ``ANALYSING`` on
        to ``COMPLETE`` both need data this scheduler does not own (the scenario, the cache
        mode, analyser output), so both are the run host's job, not this method's. On any
        failure — a scheduler-internal error or the root task raising — the lifecycle moves
        to ``FAILED`` instead, and the original exception propagates unchanged.

        Raises:
            LifecycleTransitionError: called while not in CREATED state.
            DeadlockError: no task runnable and no timer (``E-SCHED-003``).
            LivelockError: step budget exceeded (``E-SCHED-003``).
        """
        self._transition(RunState.RUNNING)
        self._loop = asyncio.get_event_loop()

        # Install the PRD §10.5 ambient patches for the duration of this run.
        guard = trap(
            seed=self._seed,
            clock=self._clock,
            strict=self._config.strict_determinism,
            on_leak=self._on_leak,
            # check_hash_seed handled at CLI start; do not double-report here.
        )
        self._guard = guard

        # Patch asyncio.sleep → virtual sleep.  This patch lives here (not in determinism.py)
        # because the redirect needs a live reference to self.sleep.
        real_asyncio_sleep = asyncio.sleep
        self._real_asyncio_sleep = real_asyncio_sleep
        _sched_self = self  # capture for the closure

        async def _virtual_asyncio_sleep(delay: float, result: object = None) -> object:
            """Redirect asyncio.sleep to the scheduler's virtual sleep."""
            await _sched_self.sleep(int(delay * 1000))
            return result

        asyncio.sleep = _virtual_asyncio_sleep  # type: ignore[assignment]

        root_task_id = _make_task_id(self._run_id, "root", 0)
        root_task = Task(
            task_id=root_task_id,
            agent_id="root",
            task_seq=0,
            coro=root_coro,
        )
        self._tasks[root_task_id] = root_task
        self._task_seq_counter["root"] = 1

        try:
            try:
                with guard:
                    self._rng = guard.seeded_random
                    await self._scheduler_loop()
            except BaseException:
                # A scheduler-internal error (deadlock, livelock, ...): the run cannot be
                # analysed, so RUNNING -> FAILED directly, not via ANALYSING.
                self._transition(RunState.FAILED)
                raise
        finally:
            asyncio.sleep = real_asyncio_sleep
            self._guard = None
            self._rng = None

        root_result = root_task.result
        exc = root_task.exception
        if exc is not None:
            # The root coroutine itself raised: same terminal state as a scheduler error,
            # for the same reason — there is nothing left to analyse.
            self._transition(RunState.FAILED)
            raise exc
        self._transition(RunState.ANALYSING)
        return root_result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Spawning tasks (called by the SDK when it creates a new agent step)
    # ------------------------------------------------------------------

    def spawn(
        self,
        coro: Coroutine[object, object, object],
        *,
        agent_id: str,
        virtual_ready_ts_ms: int = 0,
    ) -> str:
        """Register a new task with the scheduler.  Returns the task_id.

        Args:
            coro: The coroutine to run.
            agent_id: The agent this task belongs to.
            virtual_ready_ts_ms: When the task becomes eligible (0 = immediately).

        Returns:
            The ``task_id`` string for this task.
        """
        seq = self._task_seq_counter.get(agent_id, 0)
        self._task_seq_counter[agent_id] = seq + 1
        task_id = _make_task_id(self._run_id, agent_id, seq)
        task = Task(
            task_id=task_id,
            agent_id=agent_id,
            task_seq=seq,
            coro=coro,
            virtual_ready_ts_ms=virtual_ready_ts_ms,
        )
        self._tasks[task_id] = task
        return task_id

    # ------------------------------------------------------------------
    # Stamping (delegates to the recorder — the single stamping boundary)
    # ------------------------------------------------------------------

    def stamp(self, draft: DraftEvent, causes: Sequence[int] = ()) -> Event:
        """Stamp and write a draft event.  Delegates entirely to ``_recorder.write``.

        This method exists so external code (e.g., the run host) has a named interface
        without importing ``_SchedulerRecorder`` directly.  The implementation is one line.
        """
        return self._recorder.write(draft, causes)

    # -- Internal: lifecycle --------------------------------------------------------

    def _transition(self, to: RunState) -> None:
        """Move to ``to`` or raise ``LifecycleTransitionError``.

        Raises:
            LifecycleTransitionError: the ``(current, to)`` pair is not in ``_LEGAL_TRANSITIONS``.
        """
        pair = (self._state, to)
        if pair not in _LEGAL_TRANSITIONS:
            raise LifecycleTransitionError(self._state, to)
        self._state = to

    # ------------------------------------------------------------------
    # Internal: the scheduler loop (PRD §24.5)
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        """The main scheduler loop.

        Invariant: on every iteration, either a task makes progress (step counter advances)
        or the virtual clock advances (timer fires).  If neither happens, it is a deadlock.
        """
        last_clock_ms = self._clock.now_ms()
        steps_at_last_clock_advance = 0

        while self._has_remaining_tasks():
            runnable = self._collect_runnable()

            if not runnable:
                # Nothing runnable: check for timers.
                if not self._timers:
                    raise DeadlockError(
                        {
                            t.task_id: t.wait_reason
                            for t in self._tasks.values()
                            if t.state not in (TaskState.DONE,)
                        }
                    )
                # Advance clock to the earliest timer.
                earliest = min(self._timers.values())  # not set iteration — dict.values()
                self._clock.advance_to(earliest)
                # Unblock any tasks whose timer has now fired.
                self._unblock_timers()
                continue

            # Check step budget (livelock guard).
            now_ms = self._clock.now_ms()
            if now_ms > last_clock_ms:
                last_clock_ms = now_ms
                steps_at_last_clock_advance = self._step
            elif self._step - steps_at_last_clock_advance >= self._config.step_budget:
                raise LivelockError(self._config.step_budget, self._step)

            # P09 hook: inspect runnable set before choosing.
            self._fault_hook.pre_schedule(self._step, runnable)

            # Choose deterministically.
            chosen = self._choose(runnable)

            # Advance the step counter and resume.
            self._step += 1
            await self._resume_task(chosen)

    def _has_remaining_tasks(self) -> bool:
        """Return True while any task has not reached ``TaskState.DONE``."""
        return any(t.state is not TaskState.DONE for t in self._tasks.values())

    def _collect_runnable(self) -> list[Task]:
        """Return runnable tasks, sorted by the stable total key.

        **Sorted by explicit key — no dict/set/object iteration.**
        ``dict.values()`` returns values in insertion order (CPython 3.7+), but the
        sort below makes the final order independent of that insertion order entirely.
        """
        now = self._clock.now_ms()
        runnable = [
            t
            for t in self._tasks.values()
            if t.state in (TaskState.PENDING, TaskState.RUNNABLE) and t.virtual_ready_ts_ms <= now
        ]
        # Sort by the total key (virtual_ready_ts_ms, agent_id, task_seq) — all three
        # are deterministic; no hash value, no object identity.
        runnable.sort(key=Task.sort_key)
        return runnable

    def _choose(self, runnable: list[Task]) -> Task:
        """Return the chosen task — the **only** scheduling decision point.

        This function is a pure function of ``(rng state, sched_step, runnable)``.
        ``runnable`` is already sorted (``_collect_runnable`` ensures this) so the
        ``randrange`` call maps to the same task regardless of dict insertion order.

        Args:
            runnable: Non-empty, already sorted by ``Task.sort_key``.

        Returns:
            The chosen task.
        """
        if self._delay_schedule and self._step in self._delay_schedule:
            # Exploration override (P13): pick by index modulo count.
            return runnable[self._delay_schedule[self._step] % len(runnable)]
        if self._policy == "priority":
            return runnable[0]
        # Seeded random choice — uses the SAME rng stream as user random.random() calls.
        assert self._rng is not None  # noqa: S101 — always set inside run()
        return runnable[self._rng.randrange(len(runnable))]

    async def _resume_task(self, task: Task) -> None:
        """Resume ``task`` until its next yield point.

        The sequence:
        1. Emit a ``schedule_decision`` event (stamped here, under the scheduler's own
           execution context).
        2. Bind the scheduler task context (``context.py``) so the task's coroutine can
           read ``current_task()`` from within a patched builtin.
        3. Resolve the task's yield Future (or start the coroutine for the first time).
        4. Drive the asyncio event loop one step so the task runs to its next yield.
        5. Catch a clean return or exception, mark the task done.

        **Stamping note:** the ``schedule_decision`` event is stamped inside
        ``_recorder.write``, which is called before we yield to the task.  Every other
        event the task emits (spans, state reads, etc.) goes through the same
        ``_recorder.write``.  This is the single stamping point.
        """
        task.state = TaskState.RUNNING

        # Emit schedule_decision (PRD §10.2 — one per chosen task per step).
        runnable_ids = sorted(
            t.task_id
            for t in self._tasks.values()
            if t.state in (TaskState.PENDING, TaskState.RUNNABLE)
        )
        ready_ts = task.virtual_ready_ts_ms
        self._emit_schedule_decision(
            chosen_task_id=task.task_id,
            ready_task_ids=runnable_ids,
            reason=task.wait_reason or "initial",
            virtual_ready_ts_ms=ready_ts,
        )

        ctx = SchedTaskContext(task_id=task.task_id, agent_id=task.agent_id)
        with use_task(ctx):
            if task.state is not TaskState.RUNNING:
                # A fault hook changed the state; respect it.
                return

            fut = self._task_futures.get(task.task_id)
            if fut is not None:
                # Task was blocked at a yield point; resolve its Future to resume it.
                if not fut.done():
                    fut.set_result(None)
                del self._task_futures[task.task_id]
                # Dispatch tick: let the real event loop run the now-resolved future's
                # continuation. Must be the *real* asyncio.sleep, captured before run()
                # patched the module-level name to virtual — awaiting the patched name
                # here would treat this scheduler-internal tick as task's own sleep(0).
                await self._real_asyncio_sleep(0)
            else:
                # First time: start the coroutine by sending None.
                coro_task = asyncio.ensure_future(self._drive_coro(task))
                await self._real_asyncio_sleep(0)  # dispatch tick — see note above
                _ = coro_task  # prevent "coroutine was never awaited" warning

    async def _drive_coro(self, task: Task) -> None:
        """Drive ``task.coro`` to completion, catching its result or exception.

        This wrapper exists so we can catch the return value and exception from a
        coroutine that is running as an asyncio Task, not directly awaited.
        """
        ctx = SchedTaskContext(task_id=task.task_id, agent_id=task.agent_id)
        with use_task(ctx):
            try:
                task.result = await task.coro
            except Exception as exc:  # noqa: BLE001
                task.exception = exc
                self._fault_hook.on_task_done(task.task_id, exc)
            else:
                self._fault_hook.on_task_done(task.task_id, None)
            finally:
                task.state = TaskState.DONE
                # If the task is still in the futures map, resolve it so nobody waits forever.
                fut = self._task_futures.pop(task.task_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(None)

    # ------------------------------------------------------------------
    # Internal: timer management
    # ------------------------------------------------------------------

    def _unblock_timers(self) -> None:
        """Move tasks whose timer has fired back to RUNNABLE.

        Iterates ``self._timers`` sorted by task_id (not by value or insertion order) so
        unblocking order is deterministic for tasks that share a wake timestamp.
        """
        now = self._clock.now_ms()
        # Sort by task_id to get a stable order for simultaneous fires.
        fired = sorted((tid for tid, wake in self._timers.items() if wake <= now))
        for task_id in fired:
            del self._timers[task_id]
            task = self._tasks.get(task_id)
            if task is not None and task.state is TaskState.BLOCKED:
                task.state = TaskState.RUNNABLE
                task.wait_reason = ""

    # ------------------------------------------------------------------
    # Internal: vector clock (design constraint 2)
    # ------------------------------------------------------------------

    def _compute_vclock(self, slot: str, causes: Sequence[int]) -> VClock:
        """Return the next vector clock for ``slot``, per PRD §14.2 — pure, no mutation.

        PRD §14.2's rule: a **local** event (no ``causes``) increments only its own slot —
        it must never observe another slot's counter, since nothing established a causal
        edge to it. A **send/receive/lock** event (``causes`` non-empty) merges in the
        committed vclock of every referenced seq — each slot taking the pairwise max — and
        *then* increments its own slot. There is no global shared clock anywhere in this
        method; two slots that never causally interact never share a non-zero counter.

        **Copy-on-write, no set/dict iteration.** Builds a new ``dict`` from
        ``self._vclocks.get(slot, {})``; nothing is written to scheduler state here — the
        caller commits only after validation and persistence both succeed.

        Args:
            slot: The vector-clock slot this event belongs to (``clock_slot`` or
                ``agent_id`` or ``"run"``).
            causes: Seqs this event happens-after (PRD §9.3). Each must already have a
                committed vclock in ``self._event_vclocks``.

        Returns:
            A new ``VClock`` with sorted keys — identical key order on every replay.

        Raises:
            SchedulerError: ``causes`` names a seq with no recorded vclock — it was never
                stamped by this scheduler, or referenced out of order (``E-SCHED-001``).
        """
        vc = dict(self._vclocks.get(slot, {}))
        for parent_seq in causes:
            parent_vclock = self._event_vclocks.get(parent_seq)
            if parent_vclock is None:
                detail = (
                    f"causes={list(causes)!r} references seq {parent_seq}, which has no "
                    f"recorded vclock on this scheduler — it was never stamped here, or "
                    f"was referenced before it was written"
                )
                raise SchedulerError(detail)
            for other_slot, counter in parent_vclock.items():
                vc[other_slot] = max(vc.get(other_slot, 0), counter)
        vc[slot] = vc.get(slot, 0) + 1
        # Sort keys so the serialised vclock is identical across replays (canonical form).
        return dict(sorted(vc.items()))

    def _commit_vclock(self, slot: str, vclock: VClock) -> None:
        """Record ``vclock`` as the new committed clock for ``slot``.

        Called only after the event it belongs to has been validated and persisted —
        never before. This is the sole mutator of ``self._vclocks``.
        """
        self._vclocks[slot] = vclock

    # ------------------------------------------------------------------
    # Internal: causal parents (PRD §9.3)
    # ------------------------------------------------------------------

    def _causal_parents(self, current_seq: int, causes: Sequence[int]) -> list[int]:
        """Return the causal parent seq list for this event.

        When the caller declared explicit ``causes`` (a message's send, a lock's previous
        release, a span's open — PRD §9.3), those are the causal parents, full stop — sorted
        and de-duplicated so the serialised list is identical across replays. Only when
        ``causes`` is empty (scheduler-internal events, and SDK events with no declared
        edge) does this fall back to the previous event in the log, giving a linear chain.

        Args:
            current_seq: The seq being assigned.
            causes: Seqs this event happens-after, as declared by the caller.

        Returns:
            List of causal parent seqs (may be empty for event 0 with no ``causes``).
        """
        if causes:
            return sorted(set(causes))
        if current_seq == 0:
            return []
        return [current_seq - 1]

    # ------------------------------------------------------------------
    # Internal: scheduler-internal event emission
    # ------------------------------------------------------------------

    def _emit_schedule_decision(
        self,
        *,
        chosen_task_id: str,
        ready_task_ids: list[str],
        reason: str,
        virtual_ready_ts_ms: int,
    ) -> None:
        """Emit one ``schedule_decision`` event via the recorder."""
        draft = DraftEvent(
            type=EventType.SCHEDULE_DECISION,
            payload={
                "chosen_task_id": chosen_task_id,
                "ready_task_ids": ready_task_ids,  # already sorted by _collect_runnable path
                "reason": reason,
                "virtual_ready_ts_ms": virtual_ready_ts_ms,
            },
        )
        self._recorder.write(draft)

    def _emit_nondeterminism_warning(self, report: LeakReport) -> None:
        """Emit one ``nondeterminism_warning`` event via the recorder."""
        draft = DraftEvent(
            type=EventType.NONDETERMINISM_WARNING,
            payload={
                "source": report.source,
                "detail": report.detail,
                "location": report.location,
            },
        )
        self._recorder.write(draft)

    # ------------------------------------------------------------------
    # Internal: leak callback (wired into DeterminismGuard.on_leak)
    # ------------------------------------------------------------------

    def _on_leak(self, report: LeakReport) -> None:
        """Called by ``DeterminismGuard`` when a non-fatal leak is detected.

        Emits a ``nondeterminism_warning`` event so the leak appears in the log.
        """
        import warnings

        try:
            self._emit_nondeterminism_warning(report)
        except Exception:  # noqa: BLE001
            # Never let the leak reporter crash the run.  If event emission fails
            # (e.g., writer is sealed), fall back to the Python warning.
            warnings.warn(
                f"[nondeterminism_warning] could not emit event: {report.detail}",
                NondeterminismLeakWarning,
                stacklevel=4,
            )


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _make_task_id(run_id: str, agent_id: str, task_seq: int) -> str:
    """Return a deterministic, run-scoped task identifier.

    Format: ``t_{run_id[:8]}_{agent_id}_{task_seq}`` — short enough to read in a log,
    long enough to be unique within a run.  Does not use ``uuid4`` (banned) or
    ``id()`` (address-dependent).

    Args:
        run_id: The run's identifier.
        agent_id: The agent spawning the task.
        task_seq: The monotone task counter for this agent.

    Returns:
        A stable string identifier.
    """
    return f"t_{run_id[:8]}_{agent_id}_{task_seq}"


def _current_task_id() -> str:
    """Return the task_id of the task currently holding the scheduler baton.

    Raises:
        SchedulerError: no ambient scheduler task context (``E-SCHED-001``).
    """
    from agentdx.runtime.context import (
        current_task,
    )

    ctx = current_task()
    return ctx.task_id


def make_run_id(seed: int, scenario_hash: str, graph_hash: str) -> str:
    r"""Return a deterministic run identifier from its input tuple.

    PRD §6.1: ``run_id = \"r_\" + first 5 hex digits of a content hash of the inputs``.
    Uses ``blake2b`` (the project hash standard) over the canonical JSON of the inputs.

    Args:
        seed: The run seed.
        scenario_hash: Hash of the scenario content.
        graph_hash: Hash of the graph structure.

    Returns:
        A stable ``r_`` prefixed run identifier.
    """
    import json
    from hashlib import blake2b

    material = json.dumps(
        {"seed": seed, "scenario_hash": scenario_hash, "graph_hash": graph_hash},
        sort_keys=True,
    ).encode()
    return "r_" + blake2b(material, digest_size=4).hexdigest()


__all__ = [
    "CacheHook",
    "DeadlockError",
    "FaultInjectorHook",
    "LifecycleTransitionError",
    "LivelockError",
    "RunState",
    "Scheduler",
    "SchedulerError",
    "TaskState",
    "make_run_id",
]
