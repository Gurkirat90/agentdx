"""Transport-class faults (PRD §12.2): `latency` and `message_drop` (both P0, MVP).

`message_reorder`/`message_duplicate` are P1 and out of `MVP_FAULT_TYPES` —
`registry.FaultRegistry.from_resolved_scenario` already refuses to arm them
(`FaultNotImplementedError`) before any code in this module would ever see one, so this module
implements only `latency` and `message_drop`.

**Neither MVP transport fault has a real production interception point in this build — declared,
not discovered late.** PRD §12.1 names `pre_send`/`pre_deliver` as the interception points for
this fault class. `runtime.scheduler.Scheduler` has no such hooks (only `pre_schedule`,
`pre_yield`, `on_task_done` exist — the fixed P06 surface), and a targeted grep of
`sdk/generic.py` and `sdk/providers/openai_compatible.py` (this prompt's own research) confirms
`yield_point` is called from exactly one place in the whole SDK: `openai_compatible.chat()`,
around the LLM call. Message send/receive between agents (LangGraph edges, or the generic SDK's
own message-passing) never suspends through the scheduler at all — it is plain in-process Python
control flow with no interception point a fault could hook into without a `sdk/generic.py`
(P04) change, which is out of P09's DELIVERABLES.

**What this module does instead.** `TransportFaultInjector` implements the fault engine's full,
correct, pure DECISION LOGIC for both fault types — `decide_latency`/`decide_drop` are
deterministic functions of `(armed fault, virtual time, seeded stream)` exactly like
`triggers.should_fire`, they authorise via `safety.reauthorize`, they emit the same
`fault_injected`/`fault_effect` event pair `process.CrashInjector` emits, and they resolve/record
taint through the same shared `FaultTaintTracker`. What they do **not** do is get called
automatically by anything — a caller (today, only `tests/integration/faults/`'s own harness;
tomorrow, a `sdk/generic.py` message-delivery wrapper that does not exist yet) must invoke
`decide_latency`/`decide_drop` itself at its own synthetic "a message is about to be delivered on
edge E" call site. See `docs/chaos-safety.md` §"Interception point mapping" for the full table
and the closing NOT DONE block for this gap stated plainly.

**Why this class is not a `runtime.scheduler.FaultInjectorHook` subclass.** `FaultInjectorHook`'s
contract (`pre_schedule`, `pre_yield`, `on_task_done`) is a scheduler-task lifecycle contract —
`process.CrashInjector` genuinely needs all three because crashing an agent *is* a task-lifecycle
event. Dropping a message or delaying a delivery is not; forcing this class to implement three
methods it would only ever no-op would be exactly the "no-op stub" AGENTS.md §3 forbids. Instead
it exposes `fault_id_for`/`on_event_stamped` with the *same signatures and contract* as
`FaultInjectorHook`'s methods of those names (structural duck-typing, not inheritance) so a
caller wiring this injector's own emitted events into a shared `FaultTaintTracker` can do so
identically to how `CrashInjector` does — see those two methods' docstrings below.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from agentdx.events.schema import DraftEvent, Event, EventType
from agentdx.runtime.faults import safety, triggers
from agentdx.runtime.faults.registry import (
    ArmedFault,
    FaultRegistry,
    fault_effect_payload,
    fault_effect_span_id,
    param_int,
    param_str,
    params_payload,
)
from agentdx.runtime.faults.taint import FaultTaintTracker
from agentdx.scenario.schema import TargetKind

_DOCS: Final = "docs/chaos-safety.md"


@dataclass(frozen=True, slots=True)
class LatencyDecision:
    """The outcome of one `TransportFaultInjector.decide_latency` call.

    `extra_delay_ms` is 0 (not applied) when `armed` is `None` — a caller can always add
    `extra_delay_ms` to its own base delay unconditionally, with no branch needed for
    "no fault fired".
    """

    armed: ArmedFault | None
    extra_delay_ms: int


@dataclass(frozen=True, slots=True)
class DropDecision:
    """The outcome of one `TransportFaultInjector.decide_drop` call."""

    armed: ArmedFault | None
    dropped: bool


class TransportFaultInjector:
    """Pure decision logic + event emission for `latency` and `message_drop` (PRD §12.2).

    Constructed once per run, exactly like `process.CrashInjector`, and sharing the same run's
    `FaultRegistry`/seeded stream/taint tracker — but never registered as `Scheduler(fault_hook=
    ...)` itself (see module docstring). A caller (harness or future SDK wrapper) invokes
    `decide_latency`/`decide_drop` directly at its own message-delivery call sites.
    """

    def __init__(
        self,
        *,
        registry: FaultRegistry,
        seed: int,
        stamp: Callable[[DraftEvent, Sequence[int]], Event],
        taint: FaultTaintTracker,
    ) -> None:
        """Bind to one run's registry, seeded stream, stamping boundary and taint tracker.

        Args:
            registry: The armed faults + blast radius for this run.
            seed: The run seed, for `triggers.seeded_stream` — `latency` and `message_drop` may
                each use `PROBABILITY`-shaped params (`message_drop`'s own trigger can also be
                `ALWAYS`/`AT_VIRTUAL_TS`/`AFTER_N_MESSAGES`; `message_drop`'s *effect* itself has
                no probability param today, but a future param addition should not need a
                constructor change — same uniformity rationale as `CrashInjector.__init__`).
            stamp: The only way this module ever writes an event — matches `Scheduler.stamp`'s
                signature so a caller that *does* have a live scheduler can pass it directly, but
                is equally satisfiable by a test harness's own stand-in (see module docstring).
            taint: The run's shared `FaultTaintTracker` — the same instance every fault-class
                module in the run shares (`docs/chaos-safety.md` §"Wiring the taint tracker").
        """
        self._registry = registry
        self._stream = triggers.seeded_stream(seed)
        self._stamp = stamp
        self._taint = taint
        self._pending_direct_fault_id: str | None = None
        """Same mechanism, same synchronous-safety argument, as `CrashInjector`'s field of the
        same name — see that class's docstring."""

    # ------------------------------------------------------------------
    # latency
    # ------------------------------------------------------------------

    def _latency_faults_for(self, target_kind: TargetKind, target: str) -> tuple[ArmedFault, ...]:
        return tuple(
            f
            for f in self._registry.by_type("latency")
            if f.decl.target_kind is target_kind and f.decl.target == target
        )

    def decide_latency(
        self,
        *,
        target_kind: TargetKind,
        target: str,
        virtual_ts_ms: int,
        message_count: int | None = None,
    ) -> LatencyDecision:
        """Return the extra delay (ms) a delivery to/through `target` should incur right now.

        `target_kind` is `EDGE` or `AGENT` per `FAULT_CATALOGUE["latency"].target_kinds` — a
        caller delaying a specific edge's delivery passes `EDGE`; a caller delaying everything
        an agent sends or receives passes `AGENT`. `pattern` (`constant`/`spike`/`degrade`) is
        applied here, not left to the caller: `constant` returns `delay_ms` unmodified every
        time it fires; `spike` returns `delay_ms` on the *first* fire only and 0 on every
        subsequent fire of the same fault (a single delay spike, not a sustained one — PRD
        §12.2's own word "spike" implies a single event, not a plateau); `degrade` returns
        `delay_ms * fire_count` (linearly worsening latency, PRD §12.2's word "degrade" implies
        monotonic worsening, not a fixed added cost) — both `spike` and `degrade` are rulings
        recorded here (not PRD-literal formulas; PRD §12.2 names the three patterns but does not
        give their formulas) and restated in `docs/chaos-safety.md`. `jitter_ms` is intentionally
        never applied: adding it would require a random draw, and `jitter_ms` describes a
        *range* (PRD §12.2: "jitter_ms") with no documented distribution — applying a fixed
        `jitter_ms` would misrepresent jitter as a constant, and drawing one would need a second
        RNG stream this module has no PRD-given seeding rule for; declared, not silently guessed.

        Returns:
            A `LatencyDecision` with `extra_delay_ms=0` and `armed=None` if no `latency` fault
            targeting `(target_kind, target)` is due.
        """
        for armed in self._latency_faults_for(target_kind, target):
            if not triggers.should_fire(
                armed,
                virtual_ts_ms=virtual_ts_ms,
                stream=self._stream,
                message_count=message_count,
            ):
                continue
            safety.reauthorize(armed, self._registry.blast_radius)
            self._emit_fault_injected_if_first(armed)
            pattern = param_str(armed.decl.params, "pattern", "constant")
            delay_ms = param_int(armed.decl.params, "delay_ms", 0)
            if pattern == "spike":
                applied = delay_ms if armed.fire_count == 0 else 0
            elif pattern == "degrade":
                applied = delay_ms * (armed.fire_count + 1)
            else:
                applied = delay_ms
            armed.record_fire(virtual_ts_ms=virtual_ts_ms, target=target)
            self._emit_fault_effect(armed, effect="delay", target=target, delay_virtual_ms=applied)
            return LatencyDecision(armed=armed, extra_delay_ms=applied)
        return LatencyDecision(armed=None, extra_delay_ms=0)

    # ------------------------------------------------------------------
    # message_drop
    # ------------------------------------------------------------------

    def _drop_faults_for(self, edge: str) -> tuple[ArmedFault, ...]:
        return tuple(f for f in self._registry.by_type("message_drop") if f.decl.target == edge)

    def decide_drop(
        self, *, edge: str, virtual_ts_ms: int, message_count: int | None = None
    ) -> DropDecision:
        """Return whether a message in flight on `edge` should be dropped right now.

        `message_drop`'s own `probability_permille` param (PRD §12.2, ADR-007) is a *second*,
        independent probability layered on top of whichever `TriggerKind` armed it — e.g. an
        `always`-triggered `message_drop(probability_permille=300)` drops ~30% of messages on
        every delivery, not literally every one. This is distinct from a `PROBABILITY`-kind
        trigger (which decides *whether this evaluation counts at all*); when both are present
        (trigger kind `probability`, with a *trigger* permille, plus this fault's own effect
        permille) they compose as two independent draws from `self._stream`, in that order —
        trigger first, effect-probability second — so the stream's own determinism guarantee
        (same seed, same draw order, same outcomes) is preserved regardless of which faults are
        armed (PRD §12.3's own pseudocode evaluates a fault's trigger before any of its params).
        """
        for armed in self._drop_faults_for(edge):
            if not triggers.should_fire(
                armed,
                virtual_ts_ms=virtual_ts_ms,
                stream=self._stream,
                message_count=message_count,
            ):
                continue
            permille = param_int(armed.decl.params, "probability_permille", 1000)
            if self._stream.next_permille() >= permille:
                # Trigger was due, but this fault's own probability roll said "let it through".
                # Not a fire: no event, no record_fire (PRD §12.5's own "must be surfaced
                # prominently" is about a fault that *never* triggers at all, not about one
                # individual non-dropping roll of a fault that did fire on other deliveries).
                continue
            safety.reauthorize(armed, self._registry.blast_radius)
            self._emit_fault_injected_if_first(armed)
            armed.record_fire(virtual_ts_ms=virtual_ts_ms, target=edge)
            self._emit_fault_effect(armed, effect="drop", target=edge)
            return DropDecision(armed=armed, dropped=True)
        return DropDecision(armed=None, dropped=False)

    # ------------------------------------------------------------------
    # shared event emission + taint (same shape as process.CrashInjector)
    # ------------------------------------------------------------------

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

    def _emit_fault_effect(
        self, armed: ArmedFault, *, effect: str, target: str, delay_virtual_ms: int | None = None
    ) -> None:
        decl = armed.decl
        draft = DraftEvent(
            type=EventType.FAULT_EFFECT,
            payload=fault_effect_payload(
                fault_id=decl.fault_id,
                effect=effect,
                target=target,
                delay_virtual_ms=delay_virtual_ms,
            ),
            span_id=fault_effect_span_id(decl.fault_id, target),
        )
        self._pending_direct_fault_id = decl.fault_id
        self._stamp(draft, ())

    def fault_id_for(self, draft: DraftEvent, causal_parents: Sequence[int]) -> str | None:
        """Same contract as `FaultInjectorHook.fault_id_for`.

        See `process.CrashInjector`'s method of the same name. Duck-typed, not inherited
        (see module docstring).
        """
        direct = self._pending_direct_fault_id
        self._pending_direct_fault_id = None
        return self._taint.resolve(
            agent_id=draft.agent_id, causal_parents=causal_parents, direct_fault_id=direct
        )

    def on_event_stamped(self, event: Event) -> None:
        """Same contract as `FaultInjectorHook.on_event_stamped` — commit resolved taint."""
        self._taint.record(
            seq=event.seq,
            fault_id=event.fault_id,
            is_fault_injected=event.type is EventType.FAULT_INJECTED,
        )


__all__ = ["DropDecision", "LatencyDecision", "TransportFaultInjector"]
