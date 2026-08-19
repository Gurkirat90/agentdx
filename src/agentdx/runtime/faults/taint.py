"""Fault taint propagation (PRD §9.4) — computed from the causal graph, never a time window.

**Two mechanisms, one rule.** PRD §9.4's rule is a single definition:

```
event.fault_id = first non-null value among:
    1. the fault_id of the fault that directly produced this event
    2. the fault_id inherited from any event in causal_parents
    3. the fault_id carried on the agent's context after it observed a faulted input
    4. null
```

This module implements it twice, deliberately, for two different callers:

* `FaultTaintTracker` — **live**, incremental, used by the fault-class execution modules while
  a run is in progress. It is fed one event at a time, in `seq` order, as the scheduler stamps
  them (see `runtime.scheduler`'s `FaultInjectorHook.fault_id_for` — the one new hook this
  prompt adds, `docs/chaos-safety.md` §"The scheduler.py deviation"), and answers "what
  `fault_id` should the *next* event carry" in O(len(causal_parents)) using state it already
  holds — no re-scan of the log. This is what actually gets written to `Event.fault_id` in a
  real run.
* `compute_causal_taint` — **offline**, pure, given a whole already-written `events` sequence.
  It re-derives rules 1 and 2 from scratch by walking `causal_parents`, with no access to
  anything the live tracker knew ambiently (no agent-context state survives a log). This is
  the function `docs/chaos-safety.md` and this module's tests use to verify a produced log
  independently of the mechanism that produced it, and the one a future bundle-analysis
  consumer (no live run in progress) would use.

**Declared causal parents, not `Stamp.causal_parents`.** Rule 2 means genuinely-declared
happens-after edges (a message's send, a lock's previous release, a span's open — PRD §9.3)
— never `Scheduler._causal_parents`'s linear `[seq-1]` fallback, which it also applies
whenever the caller declared no explicit `causes`, purely to keep the hash-chain/vector-clock
unbroken (a continuity guarantee, not a causation claim). `FaultTaintTracker.resolve` is
therefore fed the *declared* parents only (see `_SchedulerRecorder.write`'s call site in
`runtime.scheduler`) — conflating the two was tried and discarded during this prompt's own
gate-G4 testing, where a bystander agent's own event (stamped with no `causes`) inherited
taint solely because it was the next event in `seq` order after a fault, which is
observably indistinguishable from a time window and violates PRD §9.4's "computed from the
causal graph, not a time window" framing in practical effect.

**A residual limitation this module cannot fully close: `compute_causal_taint` cannot make
the same distinction.** It is handed only a sealed log's `Event.causal_parents` field — the
one `Stamp` field, fallback-inclusive by design (PRD §9.3) — with no separate record of
which parents were caller-declared and which were the linear-fallback bookkeeping default.
So on a log containing fallback-derived `causal_parents` (any event stamped with empty
`causes` — `schedule_decision`, in particular, unconditionally), `compute_causal_taint` may
attribute rule-2 taint the live `FaultTaintTracker` would not have, because the live tracker
sees the pre-fallback `causes` at resolve time and the offline function structurally cannot
recover that distinction after the fact. This is a genuine architectural gap (a persisted-
schema change — recording declared and fallback parents separately — would close it, but
that is a P02 event-schema change, out of this prompt's scope) documented in
`docs/chaos-safety.md` §"Declared vs. linear-fallback causal parents" rather than silently
left for a future reader to rediscover. Prefer the live `FaultTaintTracker` (or an event log
that records the fault's own effect events, which do carry rule-1 taint precisely) over
`compute_causal_taint` when precision on this exact question matters.

**Rule 3 is a declared, partial mechanism — not silently skipped.** `events.validators.
check_cross_event`'s own docstring is explicit that rule 3 "is not checkable" from the log
alone ("That edge is not recorded in the log, so an event carrying a `fault_id` with no
tainted parent is accepted rather than reported") — `compute_causal_taint` therefore
implements rules 1 and 2 only, which is everything the validator can hold any implementation
accountable for. `FaultTaintTracker` goes further and implements rule 3 live, via genuine
per-agent context tracking (mirroring `sdk.generic`'s own `AgentContext` pattern one layer
down, per `runtime.context`'s docstring) — but `compute_causal_taint`, given only a sealed
log, cannot reconstruct that ambient state after the fact, and does not pretend to.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentdx.events.schema import Event


# ---------------------------------------------------------------------------------------
# Offline: rules 1 + 2, from a sealed log's causal_parents graph
# ---------------------------------------------------------------------------------------


def compute_causal_taint(events: Sequence[Event]) -> dict[int, str]:
    """Return `{seq: fault_id}` for every event causally downstream of a fault (rules 1 + 2).

    Pure function of `events` alone. Processes in the sequence's own order, which is always
    `seq`-ascending in a real log (I2, `seq` gapless from 0) — every `causal_parents` entry is
    `< seq` (PRD §9.6), so by the time an event is processed every parent's taint (if any) has
    already been resolved into `taint`, and the whole pass is O(events + total causal edges).

    Rule 1 ("the fault_id of the fault that directly produced this event") applies only to
    `fault_injected`/`fault_effect` events, read from `event.payload["fault_id"]` — every other
    event type's payload schema has no `fault_id` field at all (`events.schema.PAYLOAD_
    SCHEMAS`), so rule 1 cannot apply to them; their only route to a non-null `fault_id` is
    rule 2. Rule 2 ("inherited from any event in causal_parents ... holds the earliest") is
    resolved by comparing each candidate parent's *own* taint against `injected_at` (the seq
    each fault_id's `fault_injected` event was recorded at) and keeping the smallest.

    Args:
        events: A sequence of already-stamped events, in ascending `seq` order (a sealed log,
            or any prefix of one — the function never looks ahead of the current index).

    Returns:
        A mapping from `seq` to the `fault_id` that event should carry. A `seq` absent from
        the mapping carries no taint (`fault_id=None`).
    """
    taint: dict[int, str] = {}
    injected_at: dict[str, int] = {}

    for event in events:
        direct = _direct_fault_id(event)
        if direct is not None and event.type.value in ("fault_injected", "fault_effect"):
            taint[event.seq] = direct
            if event.type.value == "fault_injected":
                injected_at.setdefault(direct, event.seq)
            continue

        candidates = [taint[p] for p in event.causal_parents if p in taint]
        if not candidates:
            continue
        # Rule 2: "holds the earliest" — earliest by the seq its fault_injected event landed
        # at, falling back to the fault_id string itself only if two candidates were injected
        # at literally the same seq (cannot happen for distinct faults — each fault has
        # exactly one fault_injected event — but a stable tie-break costs nothing).
        best = min(candidates, key=lambda fid: (injected_at.get(fid, event.seq), fid))
        taint[event.seq] = best

    return taint


def _direct_fault_id(event: Event) -> str | None:
    """Return `event.payload["fault_id"]` if present and a string, else `None`."""
    value = event.payload.get("fault_id")
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------------------
# Live: rules 1 + 2 + 3, incremental, fed by the scheduler as each event is stamped
# ---------------------------------------------------------------------------------------


@dataclass
class FaultTaintTracker:
    """Incremental fault-taint state for one run — the live counterpart to `compute_causal_taint`.

    Owned by the `FaultInjectorHook` implementation (`process.CrashInjector` and friends
    compose one of these, or share a single instance passed at construction — see
    `docs/chaos-safety.md` §"Wiring the taint tracker"). Two entry points:

    * `resolve(draft, causal_parents, direct_fault_id=...)` — called from the scheduler.py
      deviation's new hook, `FaultInjectorHook.fault_id_for`, **before** the event is stamped.
      Answers "what fault_id should this event get", using only state already recorded from
      strictly earlier events (never the event currently being resolved) — this keeps the
      tracker's own invariant (`seq_taint` only ever grows, never revised) trivially true.
    * `record(event)` — called **after** the event is stamped and has a real `seq`, so the
      tracker can remember its resolved taint (for future `causal_parents` lookups) and, for a
      `fault_injected` event, remember which `seq` first injected that `fault_id` (mirrors
      `compute_causal_taint`'s `injected_at`, so both mechanisms agree on "earliest").
    """

    seq_taint: dict[int, str] = field(default_factory=dict)
    injected_at: dict[str, int] = field(default_factory=dict)
    agent_taint: dict[str, str] = field(default_factory=dict)
    """Rule 3's ambient state: `agent_id -> fault_id` for an agent whose *current logical
    task* observed a tainted input. Cleared for an agent by `clear_agent` when that agent's
    task completes (PRD §9.4: "until the task completes") — the caller (the crash/transport/
    dependency injector, via the scheduler's `on_task_done` hook) is responsible for calling
    it; this class has no notion of "task" on its own, deliberately, since that concept
    belongs to `runtime.scheduler.Task`, which this module must not import (a taint tracker
    has no business depending on the scheduler's task model — only on `agent_id` strings)."""

    def resolve(
        self,
        *,
        agent_id: str | None,
        causal_parents: Sequence[int],
        direct_fault_id: str | None = None,
    ) -> str | None:
        """Return the `fault_id` the next-stamped event should carry, per PRD §9.4's 3 rules.

        Args:
            agent_id: The event's own `agent_id` (or `None` for a run-scoped event) — the key
                into `agent_taint` for rule 3.
            causal_parents: The *declared* causal parent seqs for this event — empty unless
                the caller passed explicit `causes` to `stamp`/`emit`. **Not**
                `Scheduler._causal_parents`'s own output, which additionally folds in a
                synthetic linear-chain fallback for hash-chain/vclock continuity (PRD §9.3)
                that asserts no genuine happens-before edge — see `_SchedulerRecorder.write`'s
                call site for why the two are kept distinct (feeding the fallback-inclusive
                value into rule 2 would taint scheduler-internal events and any log-adjacent
                but causally-unrelated event).
            direct_fault_id: Set by the caller when the event *being resolved* is itself a
                `fault_injected`/`fault_effect` for a fault this caller knows the id of (rule
                1) — the tracker has no way to know this on its own; only the fault-class
                execution module constructing that exact draft does.

        Returns:
            The resolved `fault_id`, or `None` if none of the 3 rules applies.
        """
        if direct_fault_id is not None:
            return direct_fault_id

        candidates = [self.seq_taint[p] for p in causal_parents if p in self.seq_taint]
        if candidates:
            return min(candidates, key=lambda fid: (self.injected_at.get(fid, 1 << 62), fid))

        if agent_id is not None and agent_id in self.agent_taint:
            return self.agent_taint[agent_id]

        return None

    def record(self, *, seq: int, fault_id: str | None, is_fault_injected: bool) -> None:
        """Remember a stamped event's resolved taint, for future `resolve` calls to see.

        Args:
            seq: The event's assigned seq.
            fault_id: The taint it was resolved to (may be `None`).
            is_fault_injected: Whether this event's type is `fault_injected` — if so and
                `fault_id` is set, records `injected_at[fault_id] = seq` the first time (PRD
                §9.4's "earliest" tie-break needs to know when each fault was first injected).
        """
        if fault_id is None:
            return
        self.seq_taint[seq] = fault_id
        if is_fault_injected:
            self.injected_at.setdefault(fault_id, seq)

    def mark_agent_tainted(self, agent_id: str, fault_id: str) -> None:
        """Record that `agent_id`'s current logical task has observed a tainted input (rule 3).

        Idempotent-ish: a second call for the same agent with a *different* fault_id keeps
        whichever was recorded first — matching rule 2/rule-3's shared "earliest" spirit,
        since an agent that has already observed one faulted input is not made "more tainted"
        by observing a second, later one.
        """
        self.agent_taint.setdefault(agent_id, fault_id)

    def clear_agent(self, agent_id: str) -> None:
        """Clear `agent_id`'s ambient taint — call when that agent's current task completes."""
        self.agent_taint.pop(agent_id, None)


def taint_summary(taint: Mapping[int, str]) -> dict[str, int]:
    """Return `{fault_id: count of tainted events}` — a small reporting helper for tests/CLI."""
    out: dict[str, int] = {}
    for fault_id in taint.values():
        out[fault_id] = out.get(fault_id, 0) + 1
    return out


__all__ = ["FaultTaintTracker", "compute_causal_taint", "taint_summary"]
