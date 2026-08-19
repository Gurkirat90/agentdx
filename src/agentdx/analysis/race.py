r"""Race detection over shared state (PRD §14.3-§14.9) — the product's trust contract.

A concurrency researcher's one non-negotiable rule, stated here because it governs every
design choice in this file: **a race detector that reports races in correct code is worse
than no detector at all**, because the first false positive it ever produces is the last
report a user trusts. This module would rather under-report a real race than invent one.
Recall is honestly reported as possibly incomplete (see `docs/race-detection.md`'s coverage
statement); precision is not negotiable (I5: precision = 1.0 on the labelled benchmark, PRD
§34.3) and is never traded for recall (Design Constraint 4).

## The pipeline

```
events -> causality.build_causality -> KeyState tracking (§14.3) -> raw conflicts (§14.4)
        -> value divergence (§14.6) -> the four guards (§14.7) -> dedupe (§14.3)
        -> Finding (§14.5 classification, I6 evidence, §9.4 fault taint)
```

`detect_conflicts` returns only **reported** findings — conflicts that survive every guard.
Suppressed conflicts are not discarded (PRD §14.7: "Suppressed conflicts are ... shown in a
collapsible 'suppressed (n)' drawer ... Silent suppression would be as untrustworthy as a
false positive") — `find_conflicts` (the lower-level entry point) returns every conflict,
reported or not, each carrying its own `suppressed_by`. `detect_conflicts` is
`tuple(c.to_finding() for c in find_conflicts(events) if c.suppressed_by is None)`,
literally; the two are never allowed to diverge because there is only one code path.

## How this module gets its vector clocks: `causality.build_causality`, which reads `Event.vclock`

Every vector-clock comparison in this file goes through `analysis.causality`, which reads
each event's already-computed `Event.vclock` field rather than re-deriving it — see that
module's docstring for the full story of why (an OP-2 audit, 2026-08-19, found the original
re-derivation trusted `Event.causal_parents`, a field that also carries a non-causal
log-continuity fallback, and that this silently defeated the flagship gate-G1 scenario the
moment a log was shaped the way the real scheduler actually produces it, not the way the
provisional fixture harness does). This module inherits `causality.py`'s trust decision
rather than making its own — "vector clock rules" is implemented once, in one place, and
race detection is one of its two consumers (the other is none yet; `analysis.timing` copies
`Event.vclock` directly too, for a different, lower-stakes purpose — see its docstring; the
two modules now agree on where the vclock comes from, which they did not before this fix).

## The four guards (PRD §14.7) — implemented as independent, composable predicates

Each guard is its own function, each independently unit-tested in both directions
(`tests/analysis/race/test_guards.py`): one test proving it suppresses the false-positive
class it exists for, one test proving it does *not* suppress a genuine conflict that happens
to share a surface feature with that class. A guard tested in only one direction is an
untested guard (mission Design Constraint 3) — `docs/race-detection.md` states in one place
which test proves which half.

- **G1 Concurrency** — the definition itself: neither access happens-before the other
  (`causality.concurrent`). Implemented as the condition under which `_raw_conflicts` emits a
  conflict at all, not as a post-hoc filter — there is no code path that reaches guard
  evaluation for a happens-before-ordered pair.
- **G2 Divergence** (`_diverges`) — suppress when both accesses hashed to the same value.
  Concurrent, *identical* writes harm nothing (§14.6): the reducer/lock guards below exist for
  when concurrency is *designed for*; this guard exists for when it is *harmless by accident*.
- **G3 Declared merge** (`_has_declared_reducer`) — suppress when the write's `reducer` field
  is non-null, or the key is in the caller-supplied `crdt_keys` set. This is the guard that
  makes `research_fanout` (a real LangGraph `Annotated[list, operator.add]` channel) report
  zero findings instead of six (`C(4,2)` pairs across four workers) — see
  `docs/race-detection.md`'s worked example. `crdt_keys` exists for state written outside a
  recognised reducer channel (PRD §14.7's "or the key matches `analysis.crdt_keys`"); it is a
  parameter here, not a config file this prompt's `DELIVERABLES` do not include — the day a
  caller wants to wire it to `agentdx.toml`, this function's signature does not change.
- **G4 Explicit synchronisation** (`_same_lock`) — suppress a `write_write` pair when both
  writes carry the same non-null `lock_id`. Scoped to `write_write` because `state_read`'s
  payload has no `lock_id` field at all (PRD §9.5) — a lock protects a *write*, and the
  schema has nothing to say about a read's lock membership. The barrier half of G4's PRD text
  ("or ordered by a barrier") does not need a separate guard here: a barrier is a real
  synchronisation primitive in `causality.build_causality`'s graph, so two barrier-ordered
  accesses already fail G1 (they are not concurrent) before G4 would ever be asked.

## Conflict classification (§14.5) — exhaustive by construction

`_raw_conflicts` has exactly two call sites that append a raw conflict — one on the
`state_write` branch (which can only ever produce `write_write` or `write_read`), one on the
`state_read` branch (which can only ever produce `read_write`). There is no third branch and
no default case: `ConflictSubtype` has exactly the three PRD §14.5 members, and the type
checker (not a runtime `else: raise`) is what makes a fourth subtype impossible to construct.

## Fault taint (PRD §9.4)

`Event.fault_id` is already the fully-resolved taint marker — `runtime.faults.taint`
(P09, `BUILT`) computes rules 1-3 once, at stamp time, so this module does not re-derive
taint; it reads the field. A `Finding` carries `fault_id` = the earlier of its two accesses'
`fault_id` values (PRD §9.4: "`fault_id` holds the earliest"), or `None` if neither is
tainted. A fault-tainted conflict is still **reported**, never suppressed — PRD §14.7 lists
four guards and fault taint is not a fifth; a race that only manifests because a fault
perturbed scheduling is still a real race the user's code needs to survive. It is
*classified* differently (mission Design Constraint: "a fault-caused conflict is classified
differently") so a caller building a scorecard or a UI can separate "this system has a
concurrency bug" from "this system has a concurrency bug that a chaos experiment surfaced" —
the distinction matters for what a user does next, not for whether it is reported.

PRD §14.3 (access tracking), §14.4 (the detection algorithm), §14.5 (classification), §14.6
(value divergence), §14.7 (the four guards), §14.8 (minimal reproduction), §14.9 (reporting
format), §9.4 (fault taint), §42.3 (this module's own risk analysis).

**I3 purity.** Imports only `agentdx.analysis.causality` (a sibling analysis module) and
`agentdx.events`. No `agentdx.runtime`, no `agentdx.sdk`, no model client, no exploration —
`analysis.explore` is not imported here and never will be (`explore/` calls `race/`, PRD
§24.3's layer table; the reverse would be a cycle, and this module does not perform bounded
schedule exploration in any case — that is P13's job, out of this prompt's scope).

**Determinism (NFR-14).** `detect_conflicts` and `find_conflicts` iterate `events` once, in
the order given, and every returned tuple is sorted by an explicit, documented key
(`_dedupe`, `detect_conflicts`) — never dict/set insertion order. `tests/analysis/race/
test_determinism.py` asserts 100 analyses of one log produce byte-identical (`repr`-equal)
findings, in identical order.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal

from agentdx.analysis.causality import CausalityGraph, build_causality, clock_slot_of, concurrent
from agentdx.events.schema import Event, EventType, PayloadValue

_DOCS: Final = "docs/race-detection.md"

#: The one `Finding.type` this module ever produces (PRD §14 heading; matches the shape
#: `scenario.assertions.Finding`/`analysis.verdict.StateConflictFinding` already expect —
#: both are `Protocol`s keying on `type == "state_conflict"`).
FINDING_TYPE: Final = "state_conflict"


class Severity(StrEnum):
    """Finding severity.

    Duplicated from `analysis.verdict.Severity` (identical five values), following the same
    precedent that module's own docstring documents for `scenario.schema.
    Severity` — a small closed enum needed by two modules that must not import each other in
    either direction is duplicated, not centralised, in this codebase (see also `analysis.
    redundancy`'s `_happens_before`). `analysis.verdict` consumes `Finding.severity` only as
    the *string* `Severity(finding.severity)` already re-parses (its `StateConflictFinding`
    Protocol types the field `str`, not this enum) — so there is no shared type to diverge on,
    only a shared *value set*, which both modules' test suites hold to PRD §18.3 independently.
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConflictSubtype(StrEnum):
    """PRD §14.5's three (and only three) conflict subtypes."""

    WRITE_WRITE = "write_write"
    READ_WRITE = "read_write"
    WRITE_READ = "write_read"


#: PRD §14.5's user-facing names, keyed by subtype.
USER_FACING_NAME: Final[Mapping[ConflictSubtype, str]] = {
    ConflictSubtype.WRITE_WRITE: "Lost update",
    ConflictSubtype.READ_WRITE: "Stale read",
    ConflictSubtype.WRITE_READ: "Dirty read",
}

#: PRD §14.5's typical-consequence text, keyed by subtype.
TYPICAL_CONSEQUENCE: Final[Mapping[ConflictSubtype, str]] = {
    ConflictSubtype.WRITE_WRITE: "One agent's work silently discarded",
    ConflictSubtype.READ_WRITE: "Agent acted on an outdated value",
    ConflictSubtype.WRITE_READ: "Downstream decision based on a value being replaced",
}

#: Base severity per subtype. `write_write` (a lost update) is CRITICAL — data is destroyed,
#: not merely stale — matching gate G1's own requirement (`fixtures/code_pipeline/
#: golden_findings.json`: "severity": "critical") and PRD §14.9's illustrative report, whose
#: prose ("Fix: declare a reducer...") describes the identical write_write case this maps to
#: CRITICAL. `read_write`/`write_read` are HIGH: a stale or dirty read is a real correctness
#: risk but does not, by itself, destroy information the way a lost update does.
_BASE_SEVERITY: Final[Mapping[ConflictSubtype, Severity]] = {
    ConflictSubtype.WRITE_WRITE: Severity.CRITICAL,
    ConflictSubtype.READ_WRITE: Severity.HIGH,
    ConflictSubtype.WRITE_READ: Severity.HIGH,
}

#: Guard names, exactly as PRD §14.7 spells them, for `Conflict.suppressed_by`.
GuardName = Literal["G2", "G3", "G4"]


class RaceAnalysisError(RuntimeError):
    """A race-detection failure, carrying a stable `E-RACE-0NN` code."""

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


class MissingRunSeedError(RaceAnalysisError):
    """Raised by `minimal_repro` when `events` has no `run_start` event to pin a seed from.

    A minimal reproduction must pin the exact seed the finding was observed under (PRD §14.8:
    `Scenario(seed=root_run.seed, ...)`) — `run_start.payload.seed` (PRD §10.10) is the only
    place that value lives on the log itself. A log with no `run_start` event has nothing to
    pin, so this fails loudly rather than emitting a scenario with a fabricated seed that would
    not, in fact, reproduce anything.
    """

    def __init__(self) -> None:
        """Build the message."""
        super().__init__(
            "E-RACE-005",
            "cannot build a minimal repro: events has no run_start event to read seed from",
        )


class UnclassifiableConflictError(RaceAnalysisError):
    """Raised if a code path ever reaches conflict construction with no matching subtype.

    This should be unreachable — `_raw_conflicts` has exactly two call sites, one per branch
    of the `state_write`/`state_read` dispatch, each passing a fixed, literal subtype. It
    exists anyway (Design Constraint 7: "an unclassifiable conflict is a bug in the
    classifier, not a new 'other' bucket") so that if a future edit to this file ever adds a
    third branch without also picking a subtype, the failure is a loud, named error instead
    of a silently-mislabelled finding.
    """

    def __init__(self, *, seq_a: int, seq_b: int) -> None:
        """Build the message naming both conflicting events."""
        super().__init__(
            "E-RACE-001",
            f"no PRD §14.5 subtype applies to the conflict between seq {seq_a} and seq "
            f"{seq_b} — this is a bug in race.py, not a new conflict shape to add a bucket for",
        )


@dataclass(frozen=True, slots=True)
class Access:
    """One `state_read`/`state_write` event, as race detection needs it (PRD §14.3).

    Guarantees: `vclock` is the *causality-graph* clock (`analysis.causality`), never a value
    read off `Event.vclock` directly (see the module docstring). `reducer`/`lock_id`/`txn_id`
    are `None` for a `state_read` (the schema has no such fields on that payload, PRD §9.5) —
    never a fabricated default standing in for "not applicable".
    """

    seq: int
    event_type: Literal[EventType.STATE_READ, EventType.STATE_WRITE]
    agent_id: str | None
    slot: str
    key: str
    vclock: Mapping[str, int]
    value_hash: str
    virtual_ts_ms: int
    fault_id: str | None
    missing: bool | None  # state_read only
    reducer: str | None  # state_write only
    lock_id: str | None  # state_write only
    txn_id: str | None  # state_write only


@dataclass(frozen=True, slots=True)
class Conflict:
    """One concurrent, divergent access pair — reported or suppressed (PRD §14.7).

    Guarantees: `read` and `write` are always populated in `(read-side, write-side)` order
    regardless of subtype — for `write_write`, `read` is unused (`None`) and `write_a`/
    `write_b` carry both writes instead (see the two constructors, `for_write_write`/
    `for_read_write_pair`, rather than one constructor with optional-everything). `divergent`
    is always PRD §14.6-computed before this object exists; a `Conflict` with `divergent=False`
    is only ever constructed already carrying `suppressed_by="G2"`, so "is this suppressed"
    never requires re-deriving divergence from the two access hashes a second time.
    """

    subtype: ConflictSubtype
    key: str
    access_a: Access
    access_b: Access
    divergent: bool
    suppressed_by: GuardName | None
    torn: bool

    @property
    def evidence_seq(self) -> tuple[int, ...]:
        """Return both accesses' `seq`, ascending — this conflict's I6 evidence."""
        return tuple(sorted({self.access_a.seq, self.access_b.seq}))

    @property
    def fault_id(self) -> str | None:
        """Return the earlier of the two accesses' `fault_id` (PRD §9.4: "holds the earliest")."""
        candidates = [
            (access.seq, access.fault_id)
            for access in (self.access_a, self.access_b)
            if access.fault_id is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda pair: pair[0])[1]

    def to_finding(self) -> Finding:
        """Build the reported `Finding` for this conflict.

        Raises:
            RaceAnalysisError: `E-RACE-002` — called on a suppressed conflict. Suppressed
                conflicts are real data (surfaced via `find_conflicts`, PRD §14.7's
                "suppressed (n)" drawer) but are never turned into a `Finding` — a `Finding`
                *is* a reported claim, by construction, so there is no `Finding.suppressed`
                flag to forget to check downstream.
        """
        if self.suppressed_by is not None:
            raise RaceAnalysisError(
                "E-RACE-002",
                f"to_finding() called on a conflict suppressed by {self.suppressed_by} — "
                f"suppressed conflicts are never findings",
            )
        severity = _severity_for(self)
        surviving, discarded = _surviving_and_discarded(self)
        return Finding(
            finding_id=_finding_id(self),
            type=FINDING_TYPE,
            subtype=self.subtype.value,
            severity=severity.value,
            key=self.key,
            agent_a=self.access_a.agent_id,
            agent_b=self.access_b.agent_id,
            seq_a=self.access_a.seq,
            seq_b=self.access_b.seq,
            evidence_seq=self.evidence_seq,
            torn=self.torn,
            fault_id=self.fault_id,
            surviving_seq=surviving,
            discarded_seq=discarded,
            title=_title(self),
            description=_description(self),
            recommendation=_recommendation(self),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """One reported race (PRD §14.5, §14.9).

    Structurally satisfies `scenario.assertions.Finding` / `analysis.verdict.
    StateConflictFinding` (`type`, `severity`, `evidence_seq`) —
    both are `Protocol`s checking exactly those three attributes; this dataclass's superset of
    fields (Design Constraint 6: both seqs, both agents, the key, the classification, fault
    taint) is what the rest of the codebase's structural typing does not need to know about
    to accept a `Finding` wherever it already expects the Protocol.

    Guarantees: `evidence_seq` is non-empty and sorted (I6); `severity` is always a valid
    `Severity(...)` value; `surviving_seq`/`discarded_seq` are populated only for
    `write_write` (the only subtype where one access's value is literally overwritten by the
    other) and are `None` otherwise — a stale or dirty read does not discard a value, so
    inventing a "discarded" seq for one would be a claim this module cannot back with evidence.
    """

    finding_id: str
    type: str
    subtype: str
    severity: str
    key: str
    agent_a: str | None
    agent_b: str | None
    seq_a: int
    seq_b: int
    evidence_seq: tuple[int, ...]
    torn: bool
    fault_id: str | None
    surviving_seq: int | None
    discarded_seq: int | None
    title: str
    description: str
    recommendation: str


@dataclass
class _KeyState:
    """Mutable per-key access-tracking state (PRD §14.3) — internal to `find_conflicts`.

    Not frozen, not exported: this is working state for one forward pass, discarded once
    `find_conflicts` returns. Memory is O(live keys x slots), per §14.3's own accounting —
    each dict here holds at most one `Access` per slot, ever, regardless of how many times
    that slot has written or read the key.
    """

    writes_by_slot: dict[str, Access] = field(default_factory=dict)
    reads_by_slot: dict[str, Access] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
# PRD §14.6 — value divergence
# ---------------------------------------------------------------------------------------------


def _diverges(a: Access, b: Access) -> bool:
    """Return whether `a` and `b` disagree on the key's value (PRD §14.6).

    Two concurrent accesses to the same key with the identical `value_hash` are not a race
    worth reporting — nothing was lost, nothing is stale in a way that matters (idempotent
    concurrency). `value_hash` is a required, non-nullable field on both `state_read` and
    `state_write` payloads (PRD §9.5), so this is a plain equality check; PRD §14.6's fourth
    row ("value_hash unavailable ... report at reduced confidence") describes a case this
    module does not implement — see `docs/race-detection.md`'s honest-recall section for why
    (the schema's `value_hash` is required, not optional, so no live code path in this build
    produces a `state_write`/`state_read` with an absent one to detect).
    """
    return a.value_hash != b.value_hash


# ---------------------------------------------------------------------------------------------
# PRD §14.7 — the four guards
# ---------------------------------------------------------------------------------------------


def _has_declared_reducer(write: Access, *, crdt_keys: frozenset[str]) -> bool:
    """Guard **G3**.

    Suppress if the write's channel has a declared reducer, or the key is a
    known CRDT key. See the module docstring's guard section for the `research_fanout` case
    this exists for, and `docs/race-detection.md` for the stated limitation (a reducer that is
    *declared* but destructive, e.g. last-write-wins under a non-null name, is currently
    suppressed too — a recall gap, not a precision gap, since it only ever suppresses, never
    fabricates a conflict; `fixtures/code_pipeline/README.md` documents why the fixture set
    was deliberately built so this distinction does not matter for gates G1/G2 today).
    """
    return write.reducer is not None or write.key in crdt_keys


def _same_lock(write_a: Access, write_b: Access) -> str | None:
    """Guard **G4** (write_write only — see module docstring for why).

    Returns the shared `lock_id` if both writes carry the same non-null one, else `None`.
    """
    if write_a.lock_id is not None and write_a.lock_id == write_b.lock_id:
        return write_a.lock_id
    return None


# ---------------------------------------------------------------------------------------------
# PRD §14.3 / §14.4 — access tracking and the detection algorithm
# ---------------------------------------------------------------------------------------------


def _access_from_event(event: Event, graph: CausalityGraph) -> Access:
    """Build an `Access` from one `state_read`/`state_write` event and its causality clock."""
    payload = event.payload
    if event.type not in (EventType.STATE_WRITE, EventType.STATE_READ):  # pragma: no cover
        raise RaceAnalysisError(
            "E-RACE-004", f"seq {event.seq}: not a state_read/state_write event ({event.type})"
        )
    is_write = event.type is EventType.STATE_WRITE
    key = payload["key"]
    if not isinstance(key, str):  # pragma: no cover - schema guarantees this structurally
        raise RaceAnalysisError("E-RACE-003", f"seq {event.seq}: payload.key is not a string")
    value_hash = payload["value_hash"]
    if not isinstance(value_hash, str):  # pragma: no cover - schema guarantee
        raise RaceAnalysisError(
            "E-RACE-003", f"seq {event.seq}: payload.value_hash is not a string"
        )
    return Access(
        seq=event.seq,
        event_type=EventType.STATE_WRITE if is_write else EventType.STATE_READ,
        agent_id=event.agent_id,
        slot=clock_slot_of(event),
        key=key,
        vclock=graph.vclock_of(event.seq),
        value_hash=value_hash,
        virtual_ts_ms=event.virtual_ts_ms,
        fault_id=event.fault_id,
        missing=_bool_field(payload, "missing") if not is_write else None,
        reducer=_str_field(payload, "reducer") if is_write else None,
        lock_id=_str_field(payload, "lock_id") if is_write else None,
        txn_id=_str_field(payload, "txn_id") if is_write else None,
    )


def _bool_field(payload: Mapping[str, PayloadValue], name: str) -> bool | None:
    value = payload.get(name)
    return value if isinstance(value, bool) else None


def _str_field(payload: Mapping[str, PayloadValue], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) else None


def _conflict(
    subtype: ConflictSubtype,
    read_access: Access | None,
    write_a: Access,
    write_b: Access | None,
    *,
    crdt_keys: frozenset[str],
) -> Conflict:
    """Build one `Conflict`, evaluating divergence (G2) and the field-level guards (G3, G4).

    `write_b`/`read_access` are mutually exclusive: `write_write` passes both writes and no
    read; `read_write`/`write_read` pass one write and the read. This one function is the
    single place `Conflict` objects are constructed, so G2/G3/G4 are each evaluated exactly
    once per conflict, never duplicated across call sites.
    """
    if subtype is ConflictSubtype.WRITE_WRITE:
        assert write_b is not None  # noqa: S101 - internal invariant, not a user-facing check
        access_a, access_b = write_a, write_b
        divergent = _diverges(write_a, write_b)
        suppressed: GuardName | None = None
        if not divergent:
            suppressed = "G2"
        elif _has_declared_reducer(write_a, crdt_keys=crdt_keys) or _has_declared_reducer(
            write_b, crdt_keys=crdt_keys
        ):
            suppressed = "G3"
        elif _same_lock(write_a, write_b) is not None:
            suppressed = "G4"
        torn = False
    elif read_access is not None:
        access_a, access_b = read_access, write_a
        divergent = _diverges(read_access, write_a)
        suppressed = "G2" if not divergent else None
        if suppressed is None and _has_declared_reducer(write_a, crdt_keys=crdt_keys):
            suppressed = "G3"
        # G4 is write_write-only (see _same_lock's docstring) - a read carries no lock_id.
        torn = suppressed is None and write_a.txn_id is not None
    else:  # pragma: no cover - _raw_conflicts never calls this shape; see the class docstring
        raise UnclassifiableConflictError(seq_a=write_a.seq, seq_b=write_a.seq)

    return Conflict(
        subtype=subtype,
        key=write_a.key,
        access_a=access_a,
        access_b=access_b,
        divergent=divergent,
        suppressed_by=suppressed,
        torn=torn,
    )


def _raw_conflicts(
    events: Sequence[Event], graph: CausalityGraph, *, crdt_keys: frozenset[str]
) -> list[Conflict]:
    """Run PRD §14.4's forward pass, returning every conflict (reported or suppressed).

    Exactly two `_conflict(...)` call shapes exist here: one under the `state_write` branch
    (which can produce `write_write` against another slot's last write, or `write_read`
    against another slot's last read), one under `state_read` (which can produce `read_write`
    against another slot's last write). This mirrors PRD §14.4's pseudocode structure exactly,
    with `vc[slot]` replaced by `graph.vclock_of(event.seq)` (see the module docstring for why
    concurrency is decided by the pre-built causality graph rather than a second, live vclock
    computation duplicated into this function).
    """
    keys: dict[str, _KeyState] = {}
    conflicts: list[Conflict] = []

    for event in events:
        if event.type not in (EventType.STATE_WRITE, EventType.STATE_READ):
            continue
        access = _access_from_event(event, graph)
        state = keys.setdefault(access.key, _KeyState())

        if event.type is EventType.STATE_WRITE:
            for other_slot, prior_write in sorted(state.writes_by_slot.items()):
                if other_slot != access.slot and concurrent(prior_write.vclock, access.vclock):
                    conflicts.append(
                        _conflict(
                            ConflictSubtype.WRITE_WRITE,
                            None,
                            prior_write,
                            access,
                            crdt_keys=crdt_keys,
                        )
                    )
            for other_slot, prior_read in sorted(state.reads_by_slot.items()):
                if other_slot != access.slot and concurrent(prior_read.vclock, access.vclock):
                    conflicts.append(
                        _conflict(
                            ConflictSubtype.WRITE_READ,
                            prior_read,
                            access,
                            None,
                            crdt_keys=crdt_keys,
                        )
                    )
            state.writes_by_slot[access.slot] = access
        else:
            for other_slot, prior_write in sorted(state.writes_by_slot.items()):
                if other_slot != access.slot and concurrent(prior_write.vclock, access.vclock):
                    conflicts.append(
                        _conflict(
                            ConflictSubtype.READ_WRITE,
                            access,
                            prior_write,
                            None,
                            crdt_keys=crdt_keys,
                        )
                    )
            state.reads_by_slot[access.slot] = access

    return conflicts


def _dedupe(conflicts: Sequence[Conflict]) -> tuple[Conflict, ...]:
    """Collapse to one representative `Conflict` per `(key, subtype, {slot_a, slot_b})`.

    PRD §14.3: "we report a representative conflict per key ... not every instance" — because
    `_KeyState` retains only the last access per slot, more than one raw conflict can be
    emitted for the same underlying pair of slots racing on the same key (e.g. slot A writes
    twice while slot B's single concurrent write is still current). The representative kept is
    the chronologically **last** one (max of the two `seq`s) — the most recent evidence of an
    ongoing conflict, matching what a user re-running the log right now would still observe.
    Suppressed and reported conflicts are deduped separately (grouping key includes
    `suppressed_by`), so a later suppressed instance never hides an earlier reported one or
    vice versa - each `suppressed_by` value tells a different story and both are preserved.
    """
    groups: dict[tuple[str, ConflictSubtype, frozenset[str], str | None], list[Conflict]] = {}
    for conflict in conflicts:
        slots = frozenset({conflict.access_a.slot, conflict.access_b.slot})
        group_key = (conflict.key, conflict.subtype, slots, conflict.suppressed_by)
        groups.setdefault(group_key, []).append(conflict)

    representatives = [
        max(group, key=lambda c: max(c.access_a.seq, c.access_b.seq))
        for _, group in sorted(groups.items(), key=lambda item: _group_sort_key(item[0]))
    ]
    return tuple(representatives)


def _group_sort_key(
    group_key: tuple[str, ConflictSubtype, frozenset[str], str | None],
) -> tuple[str, str, tuple[str, ...], str]:
    key, subtype, slots, suppressed_by = group_key
    return (key, subtype.value, tuple(sorted(slots)), suppressed_by or "")


# ---------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------


def find_conflicts(
    events: Sequence[Event], *, crdt_keys: frozenset[str] = frozenset()
) -> tuple[Conflict, ...]:
    """Return every conflict PRD §14.4's algorithm finds — reported *and* suppressed.

    This is the lower-level entry point; `detect_conflicts` (most callers want this one)
    filters to reported findings only. `find_conflicts` exists as public API so a caller that
    wants PRD §14.7's "suppressed (n)" drawer (out of this prompt's scope to render, but not
    to make possible) does not need to re-run the algorithm to get suppressed conflicts back.

    Args:
        events: The complete event log for one run, in `seq` order (passed through to
            `causality.build_causality` — see its docstring for the ordering precondition).
        crdt_keys: State keys treated as CRDT-merged regardless of a per-write `reducer`
            field — guard G3's config-free extension point (see the module docstring).

    Returns:
        Every conflict found, sorted by `(key, subtype, slots, suppressed_by)` — deterministic
        and independent of dict/set iteration order (NFR-14).
    """
    graph = build_causality(events)
    raw = _raw_conflicts(events, graph, crdt_keys=crdt_keys)
    return _dedupe(raw)


def detect_conflicts(
    events: Sequence[Event], *, crdt_keys: frozenset[str] = frozenset()
) -> tuple[Finding, ...]:
    """Return every **reported** race in `events` (PRD §14.4's `detect_conflicts`).

    A conflict survives into this list only if it is concurrent (G1, `causality.concurrent`),
    divergent (G2), has no declared reducer (G3) and shares no lock (G4). See the module
    docstring's guard section and `docs/race-detection.md` for the full account.

    Args:
        events: The complete event log for one run, in `seq` order.
        crdt_keys: See `find_conflicts`.

    Returns:
        Findings sorted identically to `find_conflicts`' conflict order (this is a filter of
        that same sorted sequence, not a re-sort).
    """
    return tuple(
        conflict.to_finding()
        for conflict in find_conflicts(events, crdt_keys=crdt_keys)
        if conflict.suppressed_by is None
    )


# ---------------------------------------------------------------------------------------------
# Finding construction helpers (severity, ids, prose) — PRD §14.5, §14.9
# ---------------------------------------------------------------------------------------------


def _severity_for(conflict: Conflict) -> Severity:
    """Return the finding's severity: base-by-subtype, elevated one step for a torn read.

    PRD §14.5: "Torn reads ... are reported as `write_read` with `torn: true` and elevated
    severity." `write_read`'s base is HIGH; elevated is CRITICAL (the next step up PRD §18.3's
    ordering, `analysis.verdict.SEVERITY_ORDER`, would use — duplicated here rather than
    imported, see `Severity`'s own docstring).
    """
    base = _BASE_SEVERITY[conflict.subtype]
    if conflict.torn and base is Severity.HIGH:
        return Severity.CRITICAL
    return base


def _surviving_and_discarded(conflict: Conflict) -> tuple[int | None, int | None]:
    """Return `(surviving_seq, discarded_seq)` for a `write_write` conflict, else `(None, None)`.

    The surviving write is whichever landed later in `seq` order — this build has no
    scheduler-independent "last write wins" rule beyond program order (PRD §23.1's own
    fixture: "whichever lands second in program order simply replaces the first"), so `seq`
    order *is* the applied order for any log this module can see (the log is a total order by
    construction, PRD §9.6).
    """
    if conflict.subtype is not ConflictSubtype.WRITE_WRITE:
        return None, None
    later, earlier = (
        (conflict.access_a, conflict.access_b)
        if conflict.access_a.seq > conflict.access_b.seq
        else (conflict.access_b, conflict.access_a)
    )
    return later.seq, earlier.seq


def _finding_id(conflict: Conflict) -> str:
    """Return a stable, human-legible finding id: `f_<key>_<seq_a>_<seq_b>`.

    Deterministic in `(key, evidence_seq)` alone — no hash, no counter, no random component
    (AGENTS.md §4.1) — so the same conflict re-detected on a re-analysis gets the identical id
    (NFR-14), and two different conflicts on the same key never collide (the seq pair is
    unique per representative, post-dedupe).
    """
    slug = "".join(ch if ch.isalnum() else "_" for ch in conflict.key)
    seq_a, seq_b = conflict.evidence_seq
    return f"f_{slug}_{seq_a}_{seq_b}"


def _title(conflict: Conflict) -> str:
    name = USER_FACING_NAME[conflict.subtype]
    return (
        f"{name} on {conflict.key} between {conflict.access_a.agent_id} and "
        f"{conflict.access_b.agent_id}"
    )


def _description(conflict: Conflict) -> str:
    a, b = conflict.access_a, conflict.access_b
    lines = [
        f"{a.agent_id} {'wrote' if a.event_type is EventType.STATE_WRITE else 'read'} "
        f"{conflict.key} at seq {a.seq}; {b.agent_id} "
        f"{'wrote' if b.event_type is EventType.STATE_WRITE else 'read'} {conflict.key} at "
        f"seq {b.seq}.",
        "Neither access happens-before the other; values diverge; no reducer or shared lock "
        "covers this key.",
    ]
    if conflict.subtype is ConflictSubtype.WRITE_WRITE:
        surviving, discarded = _surviving_and_discarded(conflict)
        lines.append(
            f"The write at seq {surviving} survived; the write at seq {discarded} was "
            f"silently discarded. {TYPICAL_CONSEQUENCE[conflict.subtype]}."
        )
    else:
        lines.append(TYPICAL_CONSEQUENCE[conflict.subtype] + ".")
    if conflict.torn:
        lines.append("This read observed a torn view of a multi-key transaction (txn_id).")
    if conflict.fault_id is not None:
        lines.append(
            f"This conflict is causally downstream of fault {conflict.fault_id} (PRD §9.4) — "
            f"classify accordingly; it is still a real conflict, not suppressed for that reason."
        )
    return " ".join(lines)


def _recommendation(conflict: Conflict) -> str:
    if conflict.subtype is ConflictSubtype.WRITE_WRITE:
        return (
            f"Declare a reducer on the `{conflict.key}` channel, take "
            f'`agentdx.lock("{conflict.key}")` around both writers, or route both writers '
            f"through a single owning agent."
        )
    return (
        f'Take `agentdx.lock("{conflict.key}")` around the read and the concurrent write, '
        f"or restructure so the reader observes `{conflict.key}` only after the writer's "
        f"branch has joined back (an explicit message or a barrier)."
    )


def format_finding(finding: Finding) -> str:
    """Render `finding` in PRD §14.9's reporting format.

    "Copy follows the source's voice rules: state what happened and what to do; never
    apologetic, never emoji-hedged" (PRD §14.9). This is a pure formatting function over an
    already-constructed `Finding` — it invents no new information.
    """
    name = USER_FACING_NAME[ConflictSubtype(finding.subtype)].upper()
    header = f"● {name:<14} {finding.key:<40} severity: {finding.severity}"
    lines = [header, f"  {finding.description}", "", f"  Fix: {finding.recommendation}"]
    lines.append(f"  Evidence: seq {', '.join(str(s) for s in finding.evidence_seq)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# PRD §14.8 — minimal reproduction
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReproScenario:
    """A minimal-reproduction scenario for one `Finding` (PRD §14.8), as scenario YAML text.

    Guarantees: `yaml_text` is a complete, self-contained scenario document (`version:` through
    `assertions:`) - not a fragment a caller must merge into something else. `path` is the
    PRD-specified destination, `scenarios/repro_<finding_id>.yaml`, for a caller that wants to
    write it there; this dataclass does not touch the filesystem itself (I3 - `analysis/` does
    not perform I/O any more than it performs imports outside its layer).

    **What this omits, and why (see also `docs/race-detection.md`'s coverage statement).** PRD
    §14.8's full algorithm identifies the two scheduling decisions that placed the accesses
    concurrently, computes a minimal `delay_schedule` that reproduces that ordering, and shrinks
    it against up to 16 re-runs. This build has no scheduler/replay component to re-run against
    at all (`analysis.explore`'s bounded schedule exploration is P13's job, PRD §15, explicitly
    out of this prompt's `DELIVERABLES`) - and the scenario schema this codebase actually ships
    (`scenario.schema.TOP_LEVEL_KEYS`) has no `delay_schedule` field yet for such a value to
    populate even if one were computed. `minimal_repro` therefore performs PRD §14.8's step 4
    only (pin `seed`, target the fixture, assert `no_state_conflicts`) and is honest about the
    other three: every fixture this build ships (`fixtures/*/README.md`) documents its races as
    reproducing under the default schedule alone, with no scheduler perturbation needed - so
    step 4 alone is a genuine, runnable reproduction for every finding this build can currently
    produce, not a stand-in that happens to work by luck.
    """

    finding_id: str
    fixture: str
    seed: int
    scenario_name: str
    path: str
    yaml_text: str


def _run_seed(events: Sequence[Event]) -> int:
    """Return `run_start.payload.seed` (PRD §10.10) - the seed a repro scenario must pin.

    Raises:
        MissingRunSeedError: `E-RACE-005` - no `run_start` event in `events`.
    """
    for event in events:
        if event.type is EventType.RUN_START:
            seed = event.payload.get("seed")
            if isinstance(seed, int) and not isinstance(seed, bool):
                return seed
            break  # pragma: no cover - schema guarantees run_start.payload.seed is an int
    raise MissingRunSeedError


def _yaml_str(value: str) -> str:
    """Render `value` as a YAML scalar, quoting only when a bare word would not round-trip.

    A small, deliberately narrow formatter - not a YAML emitter. Every string this module ever
    passes through it (a finding id, a fixture name, a scenario-relative task path) is already
    constrained to `[A-Za-z0-9_./-]` by its own producer (`_finding_id`'s slug rule, fixture
    directory names, `fixtures/.../*.md` paths), so a full `yaml.safe_dump`-style escaping
    implementation would be handling cases that cannot occur on this module's own data. It
    still checks rather than assumes: anything outside that safe set is double-quoted.
    """
    if value and all(ch.isalnum() or ch in "_./-" for ch in value):
        return value
    return json.dumps(value)  # valid YAML flow scalar syntax too


def minimal_repro(
    finding: Finding, events: Sequence[Event], *, fixture: str, task: str
) -> ReproScenario:
    """Build a minimal reproduction scenario for `finding` (PRD §14.8).

    See `ReproScenario`'s own docstring for exactly what this does and does not reproduce
    (step 4 of PRD §14.8's algorithm - pin seed, target the fixture, assert
    `no_state_conflicts` - not the delay-schedule minimisation steps 1-3, which need a
    scheduler this build does not have).

    Args:
        finding: The finding to reproduce - `finding.finding_id` names the emitted file
            (`scenarios/repro_<finding_id>.yaml`, PRD §14.8) and its `title`/`key` populate
            the description.
        events: The complete event log the finding was found in - read only for its
            `run_start` event's `seed` (PRD §10.10).
        fixture: The fixture name to target (e.g. `"code_pipeline"`) - not derivable from
            `events` or `finding` alone (PRD §9.5's `run_start` payload carries no fixture
            name; see `_run_seed`'s docstring for the one field it does carry that this
            function needs).
        task: The scenario's `task:` path (e.g. `"fixtures/tasks/refactor_module.md"`) -
            likewise caller-supplied for the same reason.

    Returns:
        A `ReproScenario` whose `yaml_text` is a complete, valid v1 scenario document.

    Raises:
        MissingRunSeedError: `E-RACE-005` - see `_run_seed`.
    """
    seed = _run_seed(events)
    scenario_name = f"repro_{finding.finding_id}"
    path = f"scenarios/{scenario_name}.yaml"
    description = (
        f"Reproduction for {finding.title}. Today, replaying this scenario's default "
        f"schedule reproduces the {finding.subtype} conflict on `{finding.key}` "
        f"(evidence: seq {', '.join(str(s) for s in finding.evidence_seq)}) and the "
        f"no_state_conflicts assertion below fails - that failure is the reproduction. "
        f"Once fixed ({finding.recommendation}), the same run passes it."
    )
    lines = [
        "# Generated by agentdx.analysis.race.minimal_repro (PRD §14.8).",
        f"# Regenerate from finding {finding.finding_id!r} rather than hand-editing seed or",
        "# assertions below; see ReproScenario's docstring for what this scenario does and",
        "# does not pin (no delay_schedule - this build's schema has no such field yet, and",
        "# this fixture's race reproduces under the default schedule alone, see its README).",
        "version: 1",
        f"scenario: {_yaml_str(scenario_name)}",
        "description: >-",
        *(f"  {chunk}" for chunk in _wrap(description, width=96)),
        "",
        "target:",
        f"  fixture: {_yaml_str(fixture)}",
        "",
        f"task: {_yaml_str(task)}",
        f"seed: {seed}",
        "",
        "assertions:",
        "  - no_state_conflicts",
    ]
    yaml_text = "\n".join(lines) + "\n"
    return ReproScenario(
        finding_id=finding.finding_id,
        fixture=fixture,
        seed=seed,
        scenario_name=scenario_name,
        path=path,
        yaml_text=yaml_text,
    )


def _wrap(text: str, *, width: int) -> list[str]:
    """Word-wrap `text` to `width` columns - a minimal local substitute for `textwrap.wrap`.

    `textwrap` is stdlib, not a forbidden import - this exists instead because `textwrap.wrap`
    collapses pre-existing whitespace idiosyncratically for this module's one caller's needs
    (a single already-normalised sentence stream); a five-line greedy wrapper is easier to
    verify by inspection than to pin down `textwrap`'s exact behaviour against.
    """
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join((*current, word))
        if current and len(candidate) > width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


__all__ = [
    "FINDING_TYPE",
    "TYPICAL_CONSEQUENCE",
    "USER_FACING_NAME",
    "Access",
    "Conflict",
    "ConflictSubtype",
    "Finding",
    "MissingRunSeedError",
    "RaceAnalysisError",
    "ReproScenario",
    "Severity",
    "UnclassifiableConflictError",
    "detect_conflicts",
    "find_conflicts",
    "format_finding",
    "minimal_repro",
]
