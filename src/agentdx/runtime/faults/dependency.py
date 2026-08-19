"""Dependency-class faults (PRD §12.2): `tool_failure` (P0, MVP). `rate_limit` is P1, deferred.

**No real production interception point, same situation as `transport.py` — see that module's
docstring for the general shape of this gap.** PRD §12.1 names `pre_tool` as `tool_failure`'s
interception point; the fixed scheduler has no such hook, and a tool call (however the SDK
eventually issues one) does not pass through `Scheduler.yield_point` today (confirmed by the
same grep `transport.py` cites — `yield_point` is called only around the LLM call in
`sdk/providers/openai_compatible.py`). `DependencyFaultInjector.decide_tool_call` is therefore
full, correct, pure decision logic, invoked by a caller (a test harness today) at its own
synthetic "a tool is about to be called" site — not by anything in the live scheduler.

**`count` semantics — a ruling, not literal PRD text (recorded here and in
`docs/chaos-safety.md`).** PRD §12.2 gives `tool_failure` a `count` param but its own prose does
not define what "count" counts. This module's ruling: `count` is the number of *consecutive*
tool calls on the armed target that fail, starting at the call whose trigger condition first
becomes true. For a one-shot trigger kind (`AT_VIRTUAL_TS`), this produces exactly `count`
failures starting at that timestamp and then stops. For `PROBABILITY` (a `REPEATING_TRIGGER_KIND`
per `triggers.py`), each independent probability draw that succeeds starts a fresh run of
`count` consecutive failures. For `ALWAYS`, every call already fails regardless of `count`
(the trigger is unconditionally true on every evaluation), so `count > 1` has no additional
observable effect beyond `count == 1` — documented rather than special-cased away, since forbidding
`count != 1` with an `always` trigger is a scenario-validation concern (P08), not this module's.

**`mode` is reported, not internally interpreted.** `timeout`/`429`/`500`/`malformed` describe
what the *caller* (the code that actually issued the tool call and now must decide how to fail
it — raise a timeout exception, return an HTTP-shaped error object, etc.) should do; this module
has no tool-calling machinery of its own to apply `mode` to. `ToolFailureDecision.mode` is the
caller's only extension point.
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

_DOCS: Final = "docs/chaos-safety.md"

_MODE_EXCEPTION_TYPE: Final[dict[str, str]] = {
    "timeout": "ToolTimeoutError",
    "429": "ToolRateLimitedError",
    "500": "ToolServerError",
    "malformed": "ToolMalformedResponseError",
}
"""`mode` -> the `exception_type` `fault_effect`'s locked schema expects (PRD §36-style typed
name, not a raw HTTP status). `mode` itself already lives on the `fault_injected` event's
`params` (PRD §12.5) — this mapping exists only because `fault_effect`'s own schema has no
`mode` field of its own to carry it a second time (see `_emit_fault_effect`)."""


@dataclass(frozen=True, slots=True)
class ToolFailureDecision:
    """The outcome of one `DependencyFaultInjector.decide_tool_call` call.

    `should_fail=False` (with `armed=None`, `mode=None`) means: let the call proceed normally.
    """

    armed: ArmedFault | None
    should_fail: bool
    mode: str | None


class DependencyFaultInjector:
    """Pure decision logic + event emission for `tool_failure` (PRD §12.2).

    Same construction shape and taint-tracker sharing as `transport.TransportFaultInjector` —
    see that class's docstring for why this is a plain class exposing `fault_id_for`/
    `on_event_stamped` rather than a `FaultInjectorHook` subclass.
    """

    def __init__(
        self,
        *,
        registry: FaultRegistry,
        seed: int,
        stamp: Callable[[DraftEvent, Sequence[int]], Event],
        taint: FaultTaintTracker,
    ) -> None:
        """Bind to one run's registry, seeded stream, stamping boundary and taint tracker."""
        self._registry = registry
        self._stream = triggers.seeded_stream(seed)
        self._stamp = stamp
        self._taint = taint
        self._remaining: dict[str, int] = {}
        """`fault_id -> remaining consecutive failures owed` — see module docstring's `count`
        ruling."""
        self._pending_direct_fault_id: str | None = None
        """Same mechanism as `process.CrashInjector`'s field of the same name."""

    def _failure_faults_for(self, tool: str) -> tuple[ArmedFault, ...]:
        return tuple(f for f in self._registry.by_type("tool_failure") if f.decl.target == tool)

    def _due_or_continuing(self, tool: str, *, virtual_ts_ms: int) -> ArmedFault | None:
        for armed in self._failure_faults_for(tool):
            fault_id = armed.decl.fault_id
            if self._remaining.get(fault_id, 0) > 0:
                return armed
            if triggers.should_fire(armed, virtual_ts_ms=virtual_ts_ms, stream=self._stream):
                count = param_int(armed.decl.params, "count", 1)
                self._remaining[fault_id] = max(count, 1)
                return armed
        return None

    def decide_tool_call(self, *, tool: str, virtual_ts_ms: int) -> ToolFailureDecision:
        """Return whether the next call to `tool` should fail, and with what `mode`.

        Consumes one unit of the fault's `count` budget per call (see module docstring's
        `count` ruling) and re-arms independently the next time its trigger condition becomes
        true again, for `REPEATING_TRIGGER_KINDS`.
        """
        armed = self._due_or_continuing(tool, virtual_ts_ms=virtual_ts_ms)
        if armed is None:
            return ToolFailureDecision(armed=None, should_fail=False, mode=None)

        safety.reauthorize(armed, self._registry.blast_radius)
        self._emit_fault_injected_if_first(armed)
        fault_id = armed.decl.fault_id
        self._remaining[fault_id] = max(self._remaining.get(fault_id, 1) - 1, 0)
        armed.record_fire(virtual_ts_ms=virtual_ts_ms, target=tool)
        mode = param_str(armed.decl.params, "mode", "timeout")
        self._emit_fault_effect(armed, target=tool, mode=mode)
        return ToolFailureDecision(armed=armed, should_fail=True, mode=mode)

    def _emit_fault_effect(self, armed: ArmedFault, *, target: str, mode: str) -> None:
        decl = armed.decl
        exception_type = _MODE_EXCEPTION_TYPE.get(mode, "ToolFailureError")
        draft = DraftEvent(
            type=EventType.FAULT_EFFECT,
            payload=fault_effect_payload(
                fault_id=decl.fault_id,
                effect="exception",
                target=target,
                exception_type=exception_type,
            ),
            span_id=fault_effect_span_id(decl.fault_id, target),
        )
        self._pending_direct_fault_id = decl.fault_id
        self._stamp(draft, ())

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

    def fault_id_for(self, draft: DraftEvent, causal_parents: Sequence[int]) -> str | None:
        """Same contract as `FaultInjectorHook.fault_id_for` — see `process.CrashInjector`."""
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


__all__ = ["DependencyFaultInjector", "ToolFailureDecision"]
