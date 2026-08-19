"""THE CONTRACT: the event model, its closed type enum, and its per-field volatility marks.

This module is the single source of truth from which four other things are *derived*:
the structural validator (`validators.py`), the canonical projection (`canonical.py`),
the human-readable contract (`docs/event-schema.md`), and the volatility property test
(`tests/unit/events/test_volatility_property.py`).

Nothing downstream maintains a parallel list of volatile fields. That is deliberate and
load-bearing: a hand-maintained exclusion list drifts from the schema, and the first
symptom of the drift is gate G3 either passing dishonestly or becoming unpassable
(PRD §10.1, §10.7; invariant I1).

Volatility is therefore a *first-class property of the schema*, not a property of the
projection. To change whether a field participates in determinism equality you edit its
`Volatility` mark here, and every derived artifact follows automatically.

PRD §9.1–9.9 (schema, closed enum, payload schemas, fault taint, ordering, validation,
versioning), §10.7 (the canonical projection), §14.2 (vector-clock representation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, TypeAlias


class ValueEnum(str, Enum):  # noqa: UP042  # D-08: is `StrEnum` on a 3.12 toolchain
    """A string enum whose `str()` is its value — `enum.StrEnum` semantics, stated explicitly.

    Guarantees: `str(member)`, `f"{member}"` and `member.value` all produce the wire value,
    so an enum member can never reach the canonical bytes as `"EventType.RUN_START"`. That
    footgun is the reason this base exists rather than a bare `(str, Enum)` mixin.

    See deviation D-08: this is `enum.StrEnum` written so that the toolchain on the current
    build host can parse it. Reverting to `StrEnum` is a mechanical, behaviour-free change
    once a Python 3.12 interpreter is available there.
    """

    def __str__(self) -> str:
        """Return the wire value, never the member's qualified name."""
        return str(self.value)


# ---------------------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------------------

SCHEMA_VERSION: Final = 2
"""The event-contract version this build writes (PRD §9.9).

A single integer. Analysers support this version and one previous, via the migration
harness in `events/migrations/`. Bumping this without an ADR is CONTEXT.md §11 tripwire 6.

Bumped 1 -> 2 at P09 OP-3 repair (D-45, closed by the row that supersedes it in
CONTEXT.md §9) to add `"aborted_guard"` to `run_end.payload.status`'s enum — see that
field's `FieldSpec` below. `events/migrations/__init__.py` carries the v1->v2 step
(purely additive: stamps `schema_version=2` on an old record, changes nothing else), and
`events/canonical.py::decode_event` applies it on read, so every committed golden fixture
keeps validating at its original bytes with no golden-file regeneration.
"""


# ---------------------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------------------

JsonScalar: TypeAlias = str | int | bool | None  # noqa: UP040  # D-08
PayloadValue: TypeAlias = (  # noqa: UP040  # D-08
    "JsonScalar | Sequence[PayloadValue] | Mapping[str, PayloadValue]"
)
"""The complete set of values a payload may contain.

`float` is deliberately absent. Floats are forbidden everywhere in the event log, not
merely discouraged: cross-platform float formatting is a determinism leak that the
canonical projection cannot normalise away, and PRD §10.6 already concedes that
"floating-point results across architectures" are not guaranteed. Every quantity in
PRD §9.5 is an integer, a hash string or an enum, so the restriction costs nothing in the
specified schema. Durations are integer milliseconds; ratios are integer per-mille.

Ruling R4 (see `docs/event-schema.md` §9). Enforced by `validators.check_structural`,
which rejects a float with `E-EVENT-013` rather than silently formatting one.
"""

VClock: TypeAlias = Mapping[str, int]  # noqa: UP040  # D-08
"""A sparse vector clock: slot name -> counter. An omitted slot reads as 0 (PRD §14.2).

Sparseness is why canonicalisation must normalise: `{"a": 1}` and `{"a": 1, "b": 0}` are
the same clock and must produce the same bytes. `canonical.normalise_vclock` does that.
"""


# ---------------------------------------------------------------------------------------
# Volatility — the mark the whole module exists to carry
# ---------------------------------------------------------------------------------------


class Volatility(ValueEnum):
    """Whether a field participates in determinism equality, and why it does not.

    Three marks rather than two. A binary volatile/stable split has no honest home for
    `run_id`, which is neither: it is perfectly reproducible in the sense that nothing
    about the machine leaks into it, yet it must differ between two executions or they
    collide on `runs.run_id PRIMARY KEY` (PRD §38). Calling it "volatile" would be a lie
    that misleads the next reader; leaving it stable makes gate G3 unpassable.

    Consumers that only care about the projection ask `in_canonical` and get a binary
    answer, so the extra mark costs the derived code nothing.
    """

    STABLE = "stable"
    """Semantically meaningful. Participates in equality. Mutating it MUST change the hash."""

    VOLATILE = "volatile"
    """Cannot be deterministic by nature — wall clock, host, pid, environment (PRD §10.7)."""

    IDENTITY = "identity"
    """Identifies the *recording*, not the run's behaviour. Differs by construction between
    two otherwise identical executions. Excluded so that G3 can compare them (ruling R1)."""

    @property
    def in_canonical(self) -> bool:
        """Return True iff a field with this mark is included in the canonical projection.

        Guarantees: this is the *only* predicate the canonical projection consults. There is
        no second list anywhere in the codebase, and adding one is a review-stopping defect.
        """
        return self is Volatility.STABLE


class FieldType(ValueEnum):
    """The closed set of shapes a schema field may take.

    Deliberately narrow. Every type here has an unambiguous canonical serialisation
    (`canonical.py`); adding a type means specifying its bytes first.
    """

    INT = "int"
    STR = "str"
    BOOL = "bool"
    INT_ARRAY = "int[]"
    STR_ARRAY = "str[]"
    VCLOCK = "vclock"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field of the event contract, with everything four derived artifacts need.

    Guarantees: `volatility` is the authoritative statement of whether this field is
    compared for determinism. `derived` is an honesty marker, not a behaviour switch —
    it is True for fields the PRD implies but does not specify, so that
    `docs/event-schema.md` can list exactly what needs owner sign-off before the freeze.
    """

    name: str
    type: FieldType
    volatility: Volatility
    required: bool = True
    nullable: bool = False
    enum: frozenset[str] | None = None
    set_valued: bool = False
    """The array has no meaningful order, so the emitter must write it sorted.

    Canonicalisation deliberately does **not** reorder arrays — a canonicaliser that
    silently sorts hides a nondeterministic emitter, and the symptom surfaces much later as
    an intermittent G3 failure that looks like a scheduler bug. Instead the structural
    validator rejects an unsorted set-valued array with `E-EVENT-028`, at write time, naming
    the field. Loud and correctly attributed beats silent and convenient.
    """

    derived: bool = False
    doc: str = ""


# ---------------------------------------------------------------------------------------
# The closed event type enum (PRD §9.3)
# ---------------------------------------------------------------------------------------


class EventType(ValueEnum):
    """The closed set of event types (PRD §9.3).

    Closed means closed: an unrecognised type is `E-EVENT-002`, never a pass-through.
    A log that could contain types the analysers do not know is a log whose findings
    cannot be trusted to be complete, which defeats the point of the append-only design.

    Adding a type is a minor version bump; removing or repurposing one is major (PRD §9.9).
    """

    RUN_START = "run_start"
    RUN_END = "run_end"
    SPAN_START = "span_start"
    SPAN_END = "span_end"
    MESSAGE_SEND = "message_send"
    MESSAGE_RECV = "message_recv"
    STATE_READ = "state_read"
    STATE_WRITE = "state_write"
    TOOL_CALL = "tool_call"
    LLM_CALL = "llm_call"
    FAULT_INJECTED = "fault_injected"
    FAULT_EFFECT = "fault_effect"
    LOCK_ACQUIRE = "lock_acquire"
    LOCK_RELEASE = "lock_release"
    BARRIER = "barrier"
    SCHEDULE_DECISION = "schedule_decision"
    INSTRUMENTATION_GAP = "instrumentation_gap"
    NONDETERMINISM_WARNING = "nondeterminism_warning"
    ASSERTION_RESULT = "assertion_result"


class EventScope(ValueEnum):
    """Whether a type belongs to a span, to the run, or may be either (PRD §9.3 "Scope")."""

    RUN = "run"
    SPAN = "span"
    RUN_OR_SPAN = "run_or_span"


EVENT_SCOPES: Final[Mapping[EventType, EventScope]] = {
    EventType.RUN_START: EventScope.RUN,
    EventType.RUN_END: EventScope.RUN,
    EventType.SPAN_START: EventScope.SPAN,
    EventType.SPAN_END: EventScope.SPAN,
    EventType.MESSAGE_SEND: EventScope.SPAN,
    EventType.MESSAGE_RECV: EventScope.SPAN,
    EventType.STATE_READ: EventScope.SPAN,
    EventType.STATE_WRITE: EventScope.SPAN,
    EventType.TOOL_CALL: EventScope.SPAN,
    EventType.LLM_CALL: EventScope.SPAN,
    EventType.FAULT_INJECTED: EventScope.RUN_OR_SPAN,
    EventType.FAULT_EFFECT: EventScope.SPAN,
    EventType.LOCK_ACQUIRE: EventScope.SPAN,
    EventType.LOCK_RELEASE: EventScope.SPAN,
    EventType.BARRIER: EventScope.SPAN,
    EventType.SCHEDULE_DECISION: EventScope.RUN,
    EventType.INSTRUMENTATION_GAP: EventScope.RUN,
    EventType.NONDETERMINISM_WARNING: EventScope.RUN,
    EventType.ASSERTION_RESULT: EventScope.RUN,
}
"""Scope per type, transcribed from PRD §9.3.

Drives the `span_id` rule in `validators.check_semantic`: required for SPAN, forbidden for
RUN, optional for RUN_OR_SPAN. Deriving it from this table rather than hardcoding a list of
"span-scoped types" is the same anti-drift discipline as the volatility marks.
"""


# ---------------------------------------------------------------------------------------
# Top-level event fields (PRD §9.2)
# ---------------------------------------------------------------------------------------

EVENT_FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec(
        "schema_version",
        FieldType.INT,
        Volatility.STABLE,
        doc="Rejects logs written by an incompatible SDK.",
    ),
    FieldSpec(
        "run_id",
        FieldType.STR,
        Volatility.IDENTITY,
        doc=(
            "`r_` + 5 hex of a content hash (PRD §6.1). IDENTITY, not stable: two replays "
            "of the same run must differ here or they collide on runs.run_id, so including "
            "it in the projection would make G3 unpassable by construction. Ruling R1."
        ),
    ),
    FieldSpec(
        "seq",
        FieldType.INT,
        Volatility.STABLE,
        doc="Assigned by the runtime under the scheduler lock. Gapless from 0, totally ordered.",
    ),
    FieldSpec(
        "sched_step",
        FieldType.INT,
        Volatility.STABLE,
        doc="Scheduler decision index. Several events may share one step.",
    ),
    FieldSpec(
        "virtual_ts_ms",
        FieldType.INT,
        Volatility.STABLE,
        doc="Virtual clock. Monotonic non-decreasing with seq. I11: unqualified time is virtual.",
    ),
    FieldSpec(
        "wall_ts_ms",
        FieldType.INT,
        Volatility.VOLATILE,
        doc="Real elapsed ms, for overhead accounting only. The archetypal volatile field.",
    ),
    FieldSpec(
        "agent_id",
        FieldType.STR,
        Volatility.STABLE,
        required=False,
        nullable=True,
        doc="null for run-scope events emitted by the runtime itself.",
    ),
    FieldSpec(
        "clock_slot",
        FieldType.STR,
        Volatility.STABLE,
        required=False,
        nullable=True,
        doc="Vector-clock slot. Defaults to agent_id; distinct for intra-agent concurrency.",
    ),
    FieldSpec(
        "vclock",
        FieldType.VCLOCK,
        Volatility.STABLE,
        doc="Sparse map, post-increment snapshot per PRD §14.2. Omitted slots are implicitly 0.",
    ),
    FieldSpec(
        "type",
        FieldType.STR,
        Volatility.STABLE,
        enum=frozenset(t.value for t in EventType),
        doc="Closed enum. An unknown type is a validation error, never a pass-through.",
    ),
    FieldSpec(
        "span_id",
        FieldType.STR,
        Volatility.STABLE,
        required=False,
        nullable=True,
        doc="Required for span-scoped types, forbidden for run-scoped types (EVENT_SCOPES).",
    ),
    FieldSpec(
        "causal_parents",
        FieldType.INT_ARRAY,
        Volatility.STABLE,
        doc="Every entry is a seq < this event's seq, so the log is topologically sorted.",
    ),
    FieldSpec(
        "fault_id",
        FieldType.STR,
        Volatility.STABLE,
        required=False,
        nullable=True,
        doc="Taint marker. Present iff causally downstream of a fault (PRD §9.4).",
    ),
)
"""The §9.2 top-level fields. `payload` is not here; it is per-type (PAYLOAD_SCHEMAS)."""


# ---------------------------------------------------------------------------------------
# Payload schemas (PRD §9.5 for nine types; the other ten derived — see docs §10)
# ---------------------------------------------------------------------------------------

_BODY_DOC = "Present only under capture_bodies=True; never instead of the hash (PRD §9.5, I8)."

PAYLOAD_SCHEMAS: Final[Mapping[EventType, tuple[FieldSpec, ...]]] = {
    # --- specified verbatim in PRD §9.5 --------------------------------------------------
    EventType.SPAN_START: (
        FieldSpec(
            "kind",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"llm_call", "tool_call", "agent_step", "handoff", "wait"}),
        ),
        FieldSpec("name", FieldType.STR, Volatility.STABLE),
        FieldSpec("parent_span_id", FieldType.STR, Volatility.STABLE, nullable=True),
        FieldSpec(
            "attributes",
            FieldType.OBJECT,
            Volatility.STABLE,
            doc="Open user-supplied map; carries retry_of for retry chains (PRD §10.9).",
        ),
    ),
    EventType.SPAN_END: (
        FieldSpec(
            "status",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"ok", "error", "crashed", "timeout", "cancelled"}),
        ),
        FieldSpec("duration_virtual_ms", FieldType.INT, Volatility.STABLE),
        FieldSpec(
            "duration_wall_ms",
            FieldType.INT,
            Volatility.VOLATILE,
            doc="Named explicitly in the PRD §10.7 exclusion list.",
        ),
        FieldSpec("error_type", FieldType.STR, Volatility.STABLE, nullable=True),
        FieldSpec(
            "error_message",
            FieldType.STR,
            Volatility.STABLE,
            nullable=True,
            doc=(
                "STABLE per PRD §10.7 ('every other field participates'). See docs §11 R-3: "
                "a repr containing a memory address would leak here despite §10.6 claiming "
                "identity is never address-derived. Flagged, not silently excluded."
            ),
        ),
    ),
    EventType.MESSAGE_SEND: (
        FieldSpec("message_id", FieldType.STR, Volatility.STABLE),
        FieldSpec("to", FieldType.STR, Volatility.STABLE),
        FieldSpec("edge", FieldType.STR, Volatility.STABLE),
        FieldSpec("payload_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("payload_bytes", FieldType.INT, Volatility.STABLE),
    ),
    EventType.MESSAGE_RECV: (
        FieldSpec("message_id", FieldType.STR, Volatility.STABLE),
        FieldSpec("from", FieldType.STR, Volatility.STABLE),
        FieldSpec("edge", FieldType.STR, Volatility.STABLE),
        FieldSpec("delivered_virtual_ts_ms", FieldType.INT, Volatility.STABLE),
        FieldSpec("reordered", FieldType.BOOL, Volatility.STABLE),
        FieldSpec("duplicate", FieldType.BOOL, Volatility.STABLE),
    ),
    EventType.STATE_READ: (
        FieldSpec("key", FieldType.STR, Volatility.STABLE),
        FieldSpec("value_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("missing", FieldType.BOOL, Volatility.STABLE),
        FieldSpec("value", FieldType.STR, Volatility.STABLE, required=False, doc=_BODY_DOC),
    ),
    EventType.STATE_WRITE: (
        FieldSpec("key", FieldType.STR, Volatility.STABLE),
        FieldSpec("value_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("prev_value_hash", FieldType.STR, Volatility.STABLE, nullable=True),
        FieldSpec("reducer", FieldType.STR, Volatility.STABLE, nullable=True),
        FieldSpec("txn_id", FieldType.STR, Volatility.STABLE, nullable=True),
        FieldSpec("lock_id", FieldType.STR, Volatility.STABLE, nullable=True),
        FieldSpec("value", FieldType.STR, Volatility.STABLE, required=False, doc=_BODY_DOC),
    ),
    EventType.TOOL_CALL: (
        FieldSpec("tool", FieldType.STR, Volatility.STABLE),
        FieldSpec("args_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("result_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("status", FieldType.STR, Volatility.STABLE, enum=frozenset({"ok", "error"})),
        FieldSpec("duration_virtual_ms", FieldType.INT, Volatility.STABLE),
        FieldSpec("args", FieldType.STR, Volatility.STABLE, required=False, doc=_BODY_DOC),
        FieldSpec("result", FieldType.STR, Volatility.STABLE, required=False, doc=_BODY_DOC),
    ),
    EventType.LLM_CALL: (
        FieldSpec("model", FieldType.STR, Volatility.STABLE),
        FieldSpec("params_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("prompt_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("response_hash", FieldType.STR, Volatility.STABLE),
        FieldSpec("prompt_tokens", FieldType.INT, Volatility.STABLE),
        FieldSpec("completion_tokens", FieldType.INT, Volatility.STABLE),
        FieldSpec(
            "cache_status",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"hit", "miss_recorded", "miss_error", "perturbed"}),
            doc=(
                "STABLE: PRD §10.1 pins 'LLM cache contents' as an input to the determinism "
                "definition, so two runs over the same cache agree here. G3 compares replays "
                "with replays, never a record run with a replay."
            ),
        ),
        FieldSpec(
            "cache_key",
            FieldType.STR,
            Volatility.STABLE,
            doc=(
                "STABLE, ruling R2. The §10.7 code pops this; the §10.7 prose exclusion list "
                "(which calls itself exhaustive) does not, and §11.4 guarantees the key "
                "carries no machine-local salt so a cache is portable between machines. "
                "Excluding it would hide a genuine prompt divergence — the failure mode that "
                "is worse than an unpassable gate, because it passes."
            ),
        ),
        FieldSpec(
            "perturbed_from_run",
            FieldType.STR,
            Volatility.IDENTITY,
            nullable=True,
            doc="A run_id, and inherits run_id's mark for the same reason (ruling R1).",
        ),
        FieldSpec("prompt", FieldType.STR, Volatility.STABLE, required=False, doc=_BODY_DOC),
        FieldSpec("response", FieldType.STR, Volatility.STABLE, required=False, doc=_BODY_DOC),
    ),
    EventType.FAULT_INJECTED: (
        FieldSpec("fault_id", FieldType.STR, Volatility.STABLE),
        FieldSpec("fault_type", FieldType.STR, Volatility.STABLE),
        FieldSpec("target", FieldType.STR, Volatility.STABLE),
        FieldSpec("params", FieldType.OBJECT, Volatility.STABLE),
        FieldSpec("trigger", FieldType.OBJECT, Volatility.STABLE),
    ),
    # --- derived: PRD §9.5 specifies no payload for these ten types ----------------------
    # Sources are named per field. Every FieldSpec below carries derived=True so that
    # docs/event-schema.md §10 can list exactly what the owner must sign off before the
    # end-of-week-1 freeze. See NOT DONE / RISKS in the P02 response.
    EventType.RUN_START: (
        FieldSpec("seed", FieldType.INT, Volatility.STABLE, derived=True, doc="PRD §10.10."),
        FieldSpec(
            "mode",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"baseline", "chaos", "replay", "explore"}),
            derived=True,
            doc="Run mode per PRD §6.1. Distinct from cache_mode.",
        ),
        FieldSpec(
            "cache_mode",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"record", "replay", "perturb", "passthrough"}),
            derived=True,
            doc="LLM cache mode per PRD §11.2. Orthogonal to mode.",
        ),
        FieldSpec("scenario_id", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
        FieldSpec("scenario_hash", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("graph_hash", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "delay_schedule_hash",
            FieldType.STR,
            Volatility.STABLE,
            derived=True,
            doc="The §14 delay-schedule signature, not the schedule itself.",
        ),
        FieldSpec("calibration_id", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
        FieldSpec("agentdx_version", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("sdk_version", FieldType.STR, Volatility.STABLE, derived=True, doc="PRD §9.9."),
        FieldSpec("model", FieldType.STR, Volatility.STABLE, derived=True, doc="PRD §11.5."),
        FieldSpec("provider_host", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("provider_sdk_version", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("host", FieldType.STR, Volatility.VOLATILE, doc="Named in PRD §10.7."),
        FieldSpec("pid", FieldType.INT, Volatility.VOLATILE, doc="Named in PRD §10.7."),
        FieldSpec("started_at_utc", FieldType.STR, Volatility.VOLATILE, doc="Named in §10.7."),
        FieldSpec("env", FieldType.OBJECT, Volatility.VOLATILE, doc="Named in PRD §10.7."),
    ),
    EventType.RUN_END: (
        FieldSpec(
            "status",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"complete", "failed", "aborted", "timeout", "aborted_guard"}),
            derived=True,
            doc=(
                "`aborted_guard` added at schema_version 2 (P09 OP-3 repair, D-45): the "
                "PRD §13.6 abort-guard outcome the scheduler's `RunState.ABORTED_GUARD` "
                "already distinguished from a plain `failed` run — see "
                "`runtime/scheduler.py::Scheduler.run`'s `except AbortGuardTripped` branch."
            ),
        ),
        FieldSpec("virtual_makespan_ms", FieldType.INT, Volatility.STABLE, derived=True),
        FieldSpec(
            "wall_makespan_ms",
            FieldType.INT,
            Volatility.VOLATILE,
            derived=True,
            doc=(
                "VOLATILE by construction and ABSENT from the PRD §10.7 exclusion list that "
                "calls itself exhaustive — the second field that would have made G3 "
                "unpassable. Found at P02, ruling R3."
            ),
        ),
        FieldSpec("event_count", FieldType.INT, Volatility.STABLE, derived=True),
        FieldSpec("total_llm_calls", FieldType.INT, Volatility.STABLE, derived=True),
        FieldSpec("total_tool_calls", FieldType.INT, Volatility.STABLE, derived=True),
        FieldSpec("total_prompt_tokens", FieldType.INT, Volatility.STABLE, derived=True),
        FieldSpec("total_completion_tokens", FieldType.INT, Volatility.STABLE, derived=True),
    ),
    EventType.FAULT_EFFECT: (
        FieldSpec("fault_id", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "effect",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"delay", "exception", "drop", "crash"}),
            derived=True,
            doc="One per MVP fault type (CONTEXT.md §3: latency, agent_crash, message_drop, "
            "tool_failure). The six P1 fault types will extend this enum in a minor bump.",
        ),
        FieldSpec("target", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "delay_virtual_ms", FieldType.INT, Volatility.STABLE, nullable=True, derived=True
        ),
        FieldSpec("exception_type", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
        FieldSpec("message_id", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
    ),
    EventType.LOCK_ACQUIRE: (
        FieldSpec("lock_id", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "wait_virtual_ms",
            FieldType.INT,
            Volatility.STABLE,
            derived=True,
            doc="Feeds the coordination-overhead bucket in PRD §16.2.",
        ),
    ),
    EventType.LOCK_RELEASE: (
        FieldSpec("lock_id", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("held_virtual_ms", FieldType.INT, Volatility.STABLE, derived=True),
    ),
    EventType.BARRIER: (
        FieldSpec("barrier_id", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "participants",
            FieldType.STR_ARRAY,
            Volatility.STABLE,
            set_valued=True,
            derived=True,
            doc="Set-valued: the emitter must write it sorted (E-EVENT-028). Use "
            "`agentdx.sorted_set()`, never bare set iteration (AGENTS.md §4.1).",
        ),
        FieldSpec(
            "phase",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"enter", "release"}),
            derived=True,
        ),
        FieldSpec("wait_virtual_ms", FieldType.INT, Volatility.STABLE, derived=True),
    ),
    EventType.SCHEDULE_DECISION: (
        FieldSpec("chosen_task_id", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "ready_task_ids",
            FieldType.STR_ARRAY,
            Volatility.STABLE,
            set_valued=True,
            derived=True,
            doc="Set-valued, same rule as barrier.participants (E-EVENT-028).",
        ),
        FieldSpec("reason", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("virtual_ready_ts_ms", FieldType.INT, Volatility.STABLE, derived=True),
    ),
    EventType.INSTRUMENTATION_GAP: (
        FieldSpec("construct", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("location", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("reason", FieldType.STR, Volatility.STABLE, derived=True),
    ),
    EventType.NONDETERMINISM_WARNING: (
        FieldSpec(
            "source",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset(
                {
                    "live_model_call",
                    "unmanaged_io",
                    "os_thread",
                    "ambient_clock",
                    "ambient_random",
                    "ambient_uuid",
                    "unordered_iteration",
                }
            ),
            derived=True,
            doc="One per row of the PRD §10.6 table that is detectable at runtime.",
        ),
        FieldSpec("detail", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec("location", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
    ),
    EventType.ASSERTION_RESULT: (
        FieldSpec("assertion_id", FieldType.STR, Volatility.STABLE, derived=True),
        FieldSpec(
            "kind",
            FieldType.STR,
            Volatility.STABLE,
            enum=frozenset({"success_check", "steady_state_hypothesis", "assertion"}),
            derived=True,
        ),
        FieldSpec("passed", FieldType.BOOL, Volatility.STABLE, derived=True),
        FieldSpec("expected", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
        FieldSpec("actual", FieldType.STR, Volatility.STABLE, nullable=True, derived=True),
    ),
}


# ---------------------------------------------------------------------------------------
# The stamping boundary (PRD §9.6, design constraint 6)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftEvent:
    """What the SDK constructs: step 1 of PRD §9.6. Carries no ordering information at all.

    Guarantees: a DraftEvent cannot be written. `EventWriter.write` accepts only `Event`,
    and the only route from DraftEvent to Event is `Event.from_draft`, which demands a
    `Stamp`. Since a Stamp can only be produced where seq and the vector clock are known —
    under the scheduler lock in P06 — the boundary is enforced by the type checker rather
    than by a comment that a later prompt can ignore.
    """

    type: EventType
    payload: Mapping[str, PayloadValue]
    agent_id: str | None = None
    clock_slot: str | None = None
    span_id: str | None = None


@dataclass(frozen=True, slots=True)
class Stamp:
    """Exactly the fields the runtime assigns under the scheduler lock (PRD §9.6 step 2).

    Guarantees: this type is the complete list of what stamping owns. If P06 needs to
    assign something not on this list, that is a schema change and a freeze violation,
    and it will show up as an edit to this dataclass rather than as a quiet extra kwarg.
    """

    seq: int
    sched_step: int
    virtual_ts_ms: int
    wall_ts_ms: int
    vclock: VClock
    causal_parents: Sequence[int]
    fault_id: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """A fully stamped, writable event — the canonical §9.2 schema.

    Guarantees: immutable (I2 begins at construction, not at the database). Constructing
    one does *not* validate it; validation is `validators.validate_event`, kept separate so
    each layer is testable on its own and so a deliberately malformed event can be built
    in a test.
    """

    schema_version: int
    run_id: str
    seq: int
    sched_step: int
    virtual_ts_ms: int
    wall_ts_ms: int
    vclock: VClock
    type: EventType
    causal_parents: Sequence[int]
    payload: Mapping[str, PayloadValue]
    agent_id: str | None = None
    clock_slot: str | None = None
    span_id: str | None = None
    fault_id: str | None = None

    @classmethod
    def from_draft(cls, draft: DraftEvent, stamp: Stamp, run_id: str) -> Event:
        """Combine an SDK draft with a runtime stamp. The only way to build a writable event.

        Guarantees: every field of `stamp` lands on the event unmodified, and no field of
        `draft` is overwritten. Does not validate — call `validators.validate_event` next,
        which is step 3 of PRD §9.6.
        """
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            seq=stamp.seq,
            sched_step=stamp.sched_step,
            virtual_ts_ms=stamp.virtual_ts_ms,
            wall_ts_ms=stamp.wall_ts_ms,
            vclock=stamp.vclock,
            type=draft.type,
            causal_parents=stamp.causal_parents,
            payload=draft.payload,
            agent_id=draft.agent_id,
            clock_slot=draft.clock_slot,
            span_id=draft.span_id,
            fault_id=stamp.fault_id,
        )


# ---------------------------------------------------------------------------------------
# Derived lookups — every consumer goes through these, nobody re-derives
# ---------------------------------------------------------------------------------------

EVENT_FIELDS_BY_NAME: Final[Mapping[str, FieldSpec]] = {f.name: f for f in EVENT_FIELDS}


def payload_fields(event_type: EventType) -> Mapping[str, FieldSpec]:
    """Return the payload field specs for a type, keyed by name.

    Guarantees: total over `EventType` — every member of the closed enum has an entry in
    PAYLOAD_SCHEMAS, asserted by `tests/unit/events/test_schema_marks.py`. Raises KeyError
    only if that test has been deleted and a type added without a payload schema.
    """
    return {f.name: f for f in PAYLOAD_SCHEMAS[event_type]}


def canonical_field_names() -> frozenset[str]:
    """Return the top-level field names that participate in determinism equality.

    Guarantees: derived solely from `Volatility.in_canonical`. This function, and its
    payload-level sibling `canonical_payload_field_names`, are the only definition of the
    projection's membership anywhere in the codebase.
    """
    return frozenset(f.name for f in EVENT_FIELDS if f.volatility.in_canonical) | {"payload"}


def canonical_payload_field_names(event_type: EventType) -> frozenset[str]:
    """Return the payload field names for a type that participate in equality.

    Guarantees: derived solely from `Volatility.in_canonical`, as above.
    """
    return frozenset(f.name for f in PAYLOAD_SCHEMAS[event_type] if f.volatility.in_canonical)


def excluded_field_paths() -> tuple[str, ...]:
    """Return every dotted path excluded from the canonical projection, sorted.

    Guarantees: this is generated, never typed by hand. `docs/event-schema.md` prints it
    and the exclusion test asserts against it, so the documented exclusion list and the
    executed one cannot disagree — which is precisely how PRD §10.7's own list came to be
    missing `run_end.payload.wall_makespan_ms`.
    """
    paths = [f.name for f in EVENT_FIELDS if not f.volatility.in_canonical]
    for event_type, specs in PAYLOAD_SCHEMAS.items():
        paths.extend(
            f"{event_type.value}.payload.{f.name}" for f in specs if not f.volatility.in_canonical
        )
    return tuple(sorted(paths))
