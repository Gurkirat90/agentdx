"""Trigger evaluation: PRD §12.3's `should_fire`, transcribed as a pure function.

**Determinism (mission Design Constraint 2).** `should_fire` is a pure function of
`(fault's declared trigger, sched_step/virtual_ts_ms, seeded stream, fault's own fired/
repeating state)`. It never reads a wall clock and never mutates its arguments. The same
scenario at the same seed evaluates `should_fire` identically at every schedule/yield point,
in the same order, so Gate G4's 20-repeat identical-outcome requirement follows from this
function's own purity plus I1 (the scheduler's existing determinism guarantee) — this module
adds no new source of non-determinism, it only has to not remove that guarantee.

**Trigger evaluation happens only at interception points, never on a timer (PRD §12.3's own
closing sentence).** This module never schedules anything and never reads virtual time except
through the `virtual_ts_ms` its caller passes in at the moment of an actual interception —
there is no polling loop here.

**Why this module does not use `random.Random` (AGENTS.md §4.1).** `random.*` is banned
under `src/agentdx/` outside the four sanctioned exceptions (`scripts/check_determinism_
hygiene.py`'s `BANNED_MODULES` — the AST scan resolves import aliases, so `from random import
Random` is caught the same as `import random`), and `runtime/faults/` is not one of the four.
The project's existing seeded RNG (`runtime.determinism.DeterminismGuard.seeded_random`) is
private scheduler state (`Scheduler._rng`), reached through no `FaultInjectorHook` argument —
and sharing that exact stream with fault-probability draws would make a `PROBABILITY`-
triggered fault's firing pattern depend on how many *scheduling* decisions happened to occur
first, an accidental coupling PRD §12.2's own "Reproducibility: Probabilities drawn from the
seeded RNG; identical under the same seed" does not ask for. Instead, `FaultRandomStream`
derives a deterministic `[0, 1000)` permille stream directly from the run seed via `blake2b`
— the project's own standard hash primitive (`events/canonical.py`, `ADR-007`), already used
project-wide for exactly this "derive something reproducible from a seed, without `random`"
shape. Same seed, same fault type counters exhausted in the same order (guaranteed by I1's
existing scheduling determinism) → the same permille sequence, every replay, every process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import blake2b
from typing import TYPE_CHECKING, Final

from agentdx.scenario.schema import TriggerKind

if TYPE_CHECKING:
    from agentdx.runtime.faults.registry import ArmedFault


@dataclass
class FaultRandomStream:
    """A deterministic `[0, 1000)` permille draw stream, seeded once per run.

    Not `random.Random` (see module docstring). `next_permille()` is the only operation:
    `blake2b(f"{seed}:{counter}")`'s leading 8 bytes, taken as a big-endian integer mod 1000.
    `counter` increments on every draw, so two consecutive draws never repeat by construction
    (a `Random`-style stream this is not — no statistical PRNG properties are claimed or
    needed beyond "one input seed produces one long, reproducible sequence of permille
    values", which is all `PROBABILITY` triggers ever consume).
    """

    seed: int
    _counter: int = field(default=0, repr=False)

    def next_permille(self) -> int:
        """Return the next value in `[0, 1000)`, advancing the stream."""
        self._counter += 1
        material = f"{self.seed}:{self._counter}".encode("ascii")
        digest = blake2b(material, digest_size=8).digest()
        return int.from_bytes(digest, "big") % 1000


def seeded_stream(seed: int) -> FaultRandomStream:
    """Return the fault engine's own seeded draw stream for one run.

    Guarantees: `seeded_stream(seed).next_permille()`, called `n` times, returns the same `n`
    values for the same `seed`, every call, every process.
    """
    return FaultRandomStream(seed=seed)


REPEATING_TRIGGER_KINDS: Final[frozenset[TriggerKind]] = frozenset(
    {TriggerKind.PROBABILITY, TriggerKind.ALWAYS}
)
"""Which `TriggerKind`s PRD §12.3's `fault.repeating` is `True` for — a ruling, not PRD text.

PRD §12.3's pseudocode reads `fault.repeating` but the scenario schema (PRD §21, `scenario/`)
declares no `repeating:` field anywhere a fault can set it — a genuine gap between §12.3 and
§21, the shape STOP-CONDITION 1 exists for. Resolved here rather than guessed at inline:
`ALWAYS` and `PROBABILITY` triggers are semantically re-evaluated on *every* interception by
design (§12.2's own catalogue: `latency`/`message_drop`/`tool_failure` with an `always` trigger
must affect every delivery/call, not just the first; a `probability` trigger is, definitionally,
an independent draw each time — PRD §12.5's own example, "a probabilistic drop that fires four
times produces four [fault_effect events]", only makes sense if the fault keeps firing). The
other four trigger kinds (`AT_VIRTUAL_TS`, `AT_SPAN_N`, `AFTER_N_MESSAGES`, `ON_STATE_WRITE`)
describe a single point in the run reached once — without one-shot gating, `AT_VIRTUAL_TS`
would fire on *every* interception point evaluated after its timestamp passes, which is
obviously wrong (PRD §12.2's own semantics for e.g. `agent_crash` describe one crash, not a
crash loop). Recorded here, in this module's own docstring, as the load-bearing ruling it is;
flagged in `docs/chaos-safety.md` and the closing NOT DONE block as a candidate PRD/schema
amendment (a `repeating:` field, or the six-line rule above written into PRD §12.3 directly).
"""


def should_fire(
    armed: ArmedFault,
    *,
    virtual_ts_ms: int,
    stream: FaultRandomStream,
    span_count: int | None = None,
    message_count: int | None = None,
    current_state_write_key: str | None = None,
) -> bool:
    """Return whether `armed`'s fault should fire right now (PRD §12.3's `should_fire`).

    Transcribed match-for-match from PRD §12.3, plus the `fired`/`repeating` gate its opening
    line names but its own pseudocode leaves as `fault.fired`/`fault.repeating` without
    defining either — `ArmedFault.fired` is this module's `fault.fired`;
    `REPEATING_TRIGGER_KINDS` is this module's `fault.repeating` (see that name's docstring for
    the ruling). The **hard invariant** ("target not in blast radius") is checked one layer
    below, by `registry.FaultRegistry.from_resolved_scenario` at arm time and again by
    `safety.reauthorize` at fire time — this function assumes it has already been armed
    against an authorised target and does not re-check membership itself, so that a single
    function's job stays exactly "is the trigger condition true", nothing more.

    Args:
        armed: The fault being evaluated. Read-only here — this function does not mutate
            `armed.fired`/`fire_count`; the caller (a fault-class execution module) does that
            only after actually applying the effect, via `armed.record_fire(...)`, so a fault
            whose trigger fired but whose effect could not be applied (e.g. the target task
            already completed) never falsely reports itself as fired.
        virtual_ts_ms: The scheduler's virtual clock reading at this interception point.
        stream: The fault engine's own seeded draw stream (`seeded_stream`) — required, never
            defaulted, so a caller cannot accidentally reach for a fresh, unseeded one.
        span_count: The target agent's completed span count, for `AT_SPAN_N` triggers. `None`
            if not applicable to this interception (the caller need not compute it for every
            call — cheap to skip when the fault's trigger kind cannot be `AT_SPAN_N`).
        message_count: The target edge's delivered message count, for `AFTER_N_MESSAGES`
            triggers. Same laziness contract as `span_count`.
        current_state_write_key: The key of the state write currently in flight, for
            `ON_STATE_WRITE` triggers. `None` outside a state-write interception.

    Returns:
        `True` if the fault should fire at this interception point.
    """
    if armed.fired and armed.decl.trigger.kind not in REPEATING_TRIGGER_KINDS:
        return False

    trigger = armed.decl.trigger
    match trigger.kind:
        case TriggerKind.AT_VIRTUAL_TS:
            assert isinstance(trigger.value, int)  # noqa: S101 — schema-guaranteed (registry.py)
            return virtual_ts_ms >= trigger.value
        case TriggerKind.AT_SPAN_N:
            assert isinstance(trigger.value, int)  # noqa: S101
            return span_count is not None and span_count == trigger.value
        case TriggerKind.AFTER_N_MESSAGES:
            assert isinstance(trigger.value, int)  # noqa: S101
            return message_count is not None and message_count >= trigger.value
        case TriggerKind.ON_STATE_WRITE:
            assert isinstance(trigger.value, str)  # noqa: S101
            return current_state_write_key is not None and current_state_write_key == trigger.value
        case TriggerKind.PROBABILITY:
            assert isinstance(trigger.value, int)  # noqa: S101 — permille, 0-1000 (ADR-007)
            return stream.next_permille() < trigger.value
        case TriggerKind.ALWAYS:
            return True
    # No trailing `raise AssertionError`: mypy proves the six `TriggerKind` members above are
    # exhaustive (a closed enum matched by literal `case`s) and flags a further fallback as
    # unreachable (`warn_unreachable`).


__all__ = ["REPEATING_TRIGGER_KINDS", "FaultRandomStream", "seeded_stream", "should_fire"]
