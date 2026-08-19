"""Builds real-scheduler-shaped event logs for `analysis.causality`/`analysis.race` tests.

**Exists to close the exact blind spot an independent OP-2 audit found (2026-08-19, see
`CONTEXT.md` §13's OP-2 repair row).** Every test in this suite used to hand-supply `vclock`
and `causal_parents` as two independent, uncoupled keyword arguments - almost always
`vclock={}` (a placeholder, left for the old, buggy `build_causality` to fill in by walking
`causal_parents`) paired with `causal_parents=()` for any event with no declared
synchronisation. That combination is not a shape the real runtime ever produces:
`runtime.scheduler.Scheduler._causal_parents` stamps a synthetic linear `[seq - 1]` fallback
on any event with empty `causes`, not `()` - and no test in this suite ever exercised that
fallback shape, which is exactly why the bug shipped invisibly.

`CausalLog` computes both fields the way a real writer does, from one input - `causes`, the
seqs this event *actually* happens-after (PRD §9.3, empty for a plain local event) - so a
test cannot accidentally construct the unrealistic shape again:

- `vclock`: `Scheduler._compute_vclock`'s algorithm (`runtime/scheduler.py`) exactly - merge
  in every named cause's already-committed vclock, elementwise-max, then bump this event's
  own slot by one. Identical to `fixtures._harness.VClockBuilder.build`.
- `causal_parents`: `Scheduler._causal_parents`'s algorithm exactly - declared `causes` if
  any (sorted, deduplicated); otherwise a linear `[seq - 1]` chain (empty only for `seq == 0`)
  - the fallback that made the old `build_causality` wrong, reproduced here on purpose so
  every test built through this class exercises it by default, not as a special case.

Not needed for `run_start`: that helper (`tests/analysis/_events.py`) hardcodes
`vclock={"_run": 1}` for a single-event log and takes no `agent_id`/`clock_slot`, so it does
not fit this class's per-slot bookkeeping - call it directly, as every test already does.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agentdx.events.schema import Event

__all__ = ["CausalLog"]


class CausalLog:
    """Accumulates events, computing each one's `vclock`/`causal_parents` like a real writer.

    One instance per test log. `seq` is assigned automatically, in emission order, starting
    at 0 - callers never pass `seq` to `add()`.
    """

    def __init__(self) -> None:
        """Start with no slot ever having been observed."""
        self._events: list[Event] = []
        self._last_vclock: dict[str, dict[str, int]] = {}

    @property
    def events(self) -> list[Event]:
        """Every event added so far, in emission (and therefore `seq`) order."""
        return list(self._events)

    def add(
        self,
        builder: Callable[..., Event],
        *,
        causes: Sequence[int] = (),
        virtual_ts_ms: int | None = None,
        **kwargs: object,
    ) -> Event:
        """Build and append one event, with a real, correctly-computed `vclock`/`causal_parents`.

        Args:
            builder: An event-builder function from `_events.py` (or a test-local one) with
                keyword parameters including `seq`, `virtual_ts_ms`, `vclock`,
                `causal_parents`, and either `agent_id` or `clock_slot`.
            causes: The seqs this event happens-after (PRD §9.3) - empty for a plain local
                event (a state op, an unsynchronised span). These are what gets merged into
                the vclock; they are also, sorted and deduplicated, what `causal_parents`
                becomes when non-empty.
            virtual_ts_ms: Defaults to this event's own `seq` (a simple, strictly-increasing
                stand-in) - pass explicitly when a test needs a specific value, e.g. a
                `message_recv`'s `delivered_virtual_ts_ms` needing to line up with something.
            **kwargs: Everything else `builder` needs (e.g. `agent_id`, `span_id`, `key`,
                `value`) - passed straight through unchanged.

        Returns:
            The built `Event`, already appended to `self.events`.
        """
        seq = len(self._events)
        slot_candidate = kwargs.get("clock_slot") or kwargs.get("agent_id")
        if not isinstance(slot_candidate, str) or not slot_candidate:
            detail = (
                "CausalLog.add needs a non-empty str agent_id or clock_slot in kwargs to "
                "know which vclock slot this event bumps - pass one explicitly for a "
                "run-scope event rather than relying on a fallback this class deliberately "
                "does not guess at"
            )
            raise ValueError(detail)
        slot: str = slot_candidate

        base = dict(self._last_vclock.get(slot, {}))
        for parent_seq in causes:
            parent = self._events[parent_seq]
            for other_slot, count in parent.vclock.items():
                if count > base.get(other_slot, 0):
                    base[other_slot] = count
        base[slot] = base.get(slot, 0) + 1
        vclock = {s: c for s, c in base.items() if c > 0}
        self._last_vclock[slot] = vclock

        # Scheduler._causal_parents's exact fallback rule (runtime/scheduler.py:1059-1078):
        # declared causes if any; otherwise a linear [seq - 1] chain, empty only at seq 0.
        causal_parents: list[int]
        if causes:
            causal_parents = sorted(set(causes))
        elif seq == 0:
            causal_parents = []
        else:
            causal_parents = [seq - 1]

        event = builder(
            seq=seq,
            virtual_ts_ms=seq if virtual_ts_ms is None else virtual_ts_ms,
            vclock=vclock,
            causal_parents=causal_parents,
            **kwargs,
        )
        self._events.append(event)
        return event
