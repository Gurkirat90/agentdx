r"""The **causality graph** (PRD §14.1, §14.2) — happens-before over a sealed event log.

**This is the causality graph, not the timing DAG (`analysis.timing`) — do not confuse them.**
`timing.py`'s own module docstring states the distinction from its side; this module states
it from this side, so the boundary is legible from either file without cross-referencing the
other. The two graphs share no code and answer different questions:

- **Causality graph** (this module) — nodes are *events*; edges are program order within a
  clock slot, `message_send -> message_recv`, `lock_release -> lock_acquire` on the same
  lock, and `barrier` participants. Answers "did A happen-before B, or were they concurrent?"
  — race detection (§14).
- **Timing DAG** (`analysis.timing`) — nodes are *leaf spans*; edges are program order,
  message causality, **observed data dependencies**, and retry links. Answers "what
  determined the elapsed time?" — critical path (§16).

**The rule that makes race detection possible at all (§14.1, restated because it is the one
mistake that silently defeats the whole module): a shared-state access — a `state_read` or
`state_write` — never contributes a causality edge.** If a write to key `k` by agent A
followed by a read of `k` by agent B were treated as ordering A before B, then by
construction no concurrent access to shared state could ever be observed as concurrent, and
`analysis.race` would report zero conflicts on every log, healthy or not — the exact silent
failure mode this module exists to prevent. Shared memory is the unsynchronised channel this
system audits; only explicit synchronisation (message passing, locks, barriers) and an
agent's own program order create ordering. This module imports **nothing** from
`analysis.timing` and must never be imported by it for anything beyond the two pure
comparison helpers `timing.py`'s own docstring already documents as deliberately
re-derived rather than shared (the same "duplicate a small pure helper across sibling
modules" precedent `analysis.redundancy._happens_before` follows for the same reason).

## Vector clock rules (PRD §14.2) — trust the runtime's own computation, not a re-derivation

PRD §14.2 states five rules: local, send, receive, lock acquire, lock release, and barrier.
Every rule has the same shape: *take the clock slot's own last known vector clock, merge in
zero or more other clocks elementwise-max, then increment the slot's own counter by one.*
What differs between the six named cases is only how many other clocks get merged in and
where they come from — local/send/lock-release merge nothing; receive/lock-acquire merge
exactly one other clock; barrier merges every other participant's, all-to-all.

**This module used to re-derive that computation from `Event.causal_parents`. That was
wrong, found by an independent OP-2 audit (2026-08-19) and fixed same day — the story is
worth keeping here because the same mistake is easy to make again.** `Event.causal_parents`
is *not* "every event this one happens-after." `runtime/scheduler.py`'s `_causal_parents`
(the sole method that computes the persisted field) returns the caller's *declared*
`causes` when present, but falls back to a synthetic linear `[seq - 1]` chain whenever
`causes` is empty — which is every `state_write`/`state_read` the SDK ever emits, since no
state operation in `sdk/generic.py` ever passes `causes` (there is nothing to declare: a
shared-state access is exactly the *unsynchronised* channel this module exists to catch, see
above). That fallback exists so the hash chain and log-continuity guarantees (PRD §9.3)
always have a provable predecessor — it is bookkeeping, not causation, and
`runtime/scheduler.py`'s own comment at the one call site that builds a `Stamp` says so in
so many words. Re-deriving a vector clock by walking `causal_parents` therefore silently
absorbed that fallback into "declared happens-after," and two concurrent, unrelated agents'
writes to the same key — the flagship lost-update scenario gate G1 is built around — computed
as `happens_before=True`. Zero conflicts, on the exact input this module exists to catch,
the moment the log is shaped the way a real run actually produces it rather than the way
`fixtures/_harness.py`'s simplified, provisional harness does (that harness's own
`FixtureRecorder.emit` never applies the fallback at all — a second, independent reason the
golden fixtures never exposed this).

**The fix: trust the persisted `Event.vclock` field directly.** It is *not* the same field as
`causal_parents`, and it does not carry the fallback — `Scheduler._compute_vclock` (the sole
method that computes it) merges in only the vclocks of seqs named in the caller's *declared*
`causes`, exactly PRD §14.2's rule, with no linear-chain substitute when `causes` is empty. A
local event's committed vclock only ever advances its own slot. `fixtures/_harness.py`'s
`VClockBuilder.build` implements the identical declared-causes-only algorithm for the
provisional fixtures, so both the real runtime and every golden fixture already do this
computation correctly — this module's job is to *read* it, once, per event, not repeat it.
This is a considered reversal of this module's own original design goal ("never trust an
upstream-stamped value blindly," still the right instinct for a precision-critical module in
general) — but the field that goal picked to distrust (`causal_parents`) was the wrong one:
it is the field that is *not* safe to trust as causation, while `Event.vclock` — the field
the original design explicitly declined to use — is the one that already is. See §13's OP-2
repair row in `CONTEXT.md` for the full audit trail, the live repro, and the priority-ordered
fix list this module's current shape closes items 1 and 3 of.

A useful side effect: this function no longer needs the *complete* run log to be correct.
The old re-derivation required every causal parent to be present in `events` or it raised
`DanglingCausalParentError` — trusting `Event.vclock` means each event's clock is already
self-contained, computed once by the runtime over the whole run, so a filtered subset of
`events` produces correct clocks for whichever seqs are included. `DanglingCausalParentError`
no longer exists; nothing in this module raises it.

## What "local" bumps when there is no agent

Run-scope events (`run_start`, `run_end`, `fault_injected`, `schedule_decision`,
`instrumentation_gap`, `nondeterminism_warning`) carry `agent_id=None` and `clock_slot=None`
(PRD §9.2: "null for run-scope events emitted by the runtime itself"). `fixtures/_harness.py`
resolves these to a synthetic `"_run"` slot (`slot = draft.clock_slot or draft.agent_id or
"_run"`); this module uses the identical fallback (`RUN_SLOT`) so a log's `run_start` and
`run_end` participate in the vector clock like any other slot rather than being silently
excluded, and so this module's output matches the real stamped log exactly (verified against
`tests/golden/*.jsonl`, whose `run_start` at seq 0 carries `vclock={"_run": 1}`).

PRD §14.2 (vector clock rules), §14.1 (the two graphs), §9.3 (`Event.vclock`'s provenance —
`Scheduler._compute_vclock`, declared `causes` only, no fallback).

**I3 purity.** Imports only `agentdx.events` (the closed event contract) and the standard
library. No `agentdx.runtime`, no `agentdx.sdk`, no model client.

**Determinism (NFR-14).** `build_causality` makes one forward pass over `events` in the order
given (the log's own `seq` order, asserted monotonic — see `NonMonotonicSeqError`); every
returned mapping is built from that single deterministic pass, no `set` iteration anywhere in
this module (`sorted(set(...))` is used at the one point a "which slots does this comparison
involve" question is asked, in `happens_before`, matching `analysis.timing`/
`analysis.redundancy`'s own idiom).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from agentdx.events.schema import Event

_DOCS: Final = "docs/race-detection.md"

#: The vector clock itself: a sparse slot -> counter map. Matches `events.schema.VClock`
#: exactly (`Mapping[str, int]`); re-declared here rather than imported so this module's
#: public surface is self-describing without a reader following an import to `events.schema`.
VClock = Mapping[str, int]

#: Fallback clock slot for a run-scope event with no `agent_id`/`clock_slot` of its own — see
#: the module docstring's "What 'local' bumps when there is no agent". Matches
#: `fixtures/_harness.py`'s `FixtureRecorder.emit` exactly, which is where this convention
#: originates (the real stamped golden logs were produced through that fallback).
RUN_SLOT: Final = "_run"


class CausalityError(RuntimeError):
    """A causality-graph construction failure, carrying a stable `E-CAUS-0NN` code.

    Same shape as `analysis.timing.TimingAnalysisError` and friends: a code plus a docs
    anchor, never a bare message.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


class NonMonotonicSeqError(CausalityError):
    """Raised when `events` is not given in strictly increasing `seq` order.

    PRD §9.6 guarantees the real event log is `seq`-ordered and topologically sorted by
    construction. Trusting `Event.vclock` directly (see the module docstring's "Vector clock
    rules" section) no longer makes this a *correctness* requirement for `build_causality`
    itself — each event's clock is self-contained, computed once by the runtime, independent
    of the order this call receives them in. It remains a precondition check on the caller
    regardless: a log that is not `seq`-ordered almost always means something upstream
    reordered or filtered the log incorrectly, and failing loudly on that is cheaper than
    letting a caller debug a downstream symptom three modules away.
    """

    def __init__(self, *, seq: int, previous_seq: int) -> None:
        """Build the message naming both the offending `seq` and what preceded it."""
        super().__init__(
            "E-CAUS-001",
            f"events must be given in strictly increasing seq order; seq {seq} followed "
            f"seq {previous_seq}",
        )


@dataclass(frozen=True, slots=True)
class CausalityGraph:
    """The causality graph for one run: every event's computed vector clock.

    Guarantees: `vclocks` has exactly one entry per event passed to `build_causality`, keyed
    by `seq`; every clock is a *sparse* map (PRD §14.2 — an absent slot reads as 0, never a
    stored 0; `build_causality` prunes zero-valued entries so two structurally-equal clocks
    are also `==`-equal regardless of which slots happened to be touched along the way);
    `slots` names the clock slot each event's own count lives on (`event.clock_slot or
    event.agent_id or RUN_SLOT` — see the module docstring).
    """

    vclocks: Mapping[int, VClock]
    slots: Mapping[int, str]

    def vclock_of(self, seq: int) -> VClock:
        """Return the computed vector clock for `seq`.

        Raises:
            KeyError: `seq` was not among the events `build_causality` was given.
        """
        return self.vclocks[seq]


def clock_slot_of(event: Event) -> str:
    """Return the clock slot `event` counts against (PRD §14.2, §8.8).

    `clock_slot` if the event carries one explicitly (set for intra-agent concurrency, PRD
    §8.8); otherwise `agent_id`; otherwise `RUN_SLOT` for a run-scope event emitted by the
    runtime itself with neither (PRD §9.2).
    """
    return event.clock_slot or event.agent_id or RUN_SLOT


def happens_before(a: VClock, b: VClock) -> bool:
    """Return whether vector clock `a` happens-before `b` (PRD §14.2, verbatim).

    `a` happens-before `b` iff every slot's count in `a` is `<=` the same slot's count in `b`,
    and at least one slot is strictly less. An absent slot reads as 0 on both sides (sparse
    maps, PRD §14.2's own rationale: "agents can be created dynamically").
    """
    at_most = all(a.get(slot, 0) <= b.get(slot, 0) for slot in a)
    strictly_less = any(a.get(slot, 0) < b.get(slot, 0) for slot in sorted(set(a) | set(b)))
    return at_most and strictly_less


def concurrent(a: VClock, b: VClock) -> bool:
    """Return whether `a` and `b` are concurrent — neither happens-before the other.

    This is the **only** definition of concurrency this codebase uses (PRD §14.2). It is a
    property of two vector clocks, not of two wall-clock or virtual-clock timestamps —
    two events with identical `virtual_ts_ms` are not necessarily concurrent, and two events
    far apart in `virtual_ts_ms` can be.
    """
    return not happens_before(a, b) and not happens_before(b, a)


def build_causality(events: Sequence[Event]) -> CausalityGraph:
    """Compute the causality graph for a `seq`-ordered run log (or an ordered subset of one).

    See the module docstring's "Vector clock rules" section for why this reads
    `event.vclock` directly rather than re-deriving it from `event.causal_parents` — the
    latter also carries a non-causal log-continuity fallback that made this exact function
    the subject of an OP-2 finding (2026-08-19). `event.vclock` is already correctly computed
    by whichever writer stamped the log (`runtime.scheduler.Scheduler._compute_vclock` for a
    real run, `fixtures._harness.VClockBuilder.build` for the provisional fixtures) from
    *declared* causes only, exactly PRD §14.2's rule — this function's only remaining job is
    pruning zero-valued slots to a sparse map and recording which slot each event counts
    against.

    Args:
        events: A `seq`-ordered run log, or an ordered subset of one. Unlike the prior
            re-deriving implementation, a filtered subset is safe: each event's `vclock` is
            self-contained, so nothing here needs another event's data to compute correctly.

    Returns:
        A `CausalityGraph` with exactly one vector clock per event given.

    Raises:
        NonMonotonicSeqError: `events` was not given in strictly increasing `seq` order.
    """
    computed: dict[int, VClock] = {}
    slots: dict[int, str] = {}

    previous_seq: int | None = None
    for event in events:
        if previous_seq is not None and event.seq <= previous_seq:
            raise NonMonotonicSeqError(seq=event.seq, previous_seq=previous_seq)
        previous_seq = event.seq

        computed[event.seq] = {s: c for s, c in event.vclock.items() if c > 0}
        slots[event.seq] = clock_slot_of(event)

    return CausalityGraph(vclocks=computed, slots=slots)


def happens_before_events(a: Event, b: Event, graph: CausalityGraph) -> bool:
    """Return whether event `a` happens-before event `b` in `graph`. Convenience wrapper."""
    return happens_before(graph.vclock_of(a.seq), graph.vclock_of(b.seq))


def concurrent_events(a: Event, b: Event, graph: CausalityGraph) -> bool:
    """Return whether events `a` and `b` are concurrent in `graph`. Convenience wrapper."""
    return concurrent(graph.vclock_of(a.seq), graph.vclock_of(b.seq))


__all__ = [
    "RUN_SLOT",
    "CausalityError",
    "CausalityGraph",
    "NonMonotonicSeqError",
    "VClock",
    "build_causality",
    "clock_slot_of",
    "concurrent",
    "concurrent_events",
    "happens_before",
    "happens_before_events",
]
