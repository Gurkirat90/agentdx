"""Layered event validation (PRD §9.8), driven entirely by the marks in `schema.py`.

Three layers, each independently callable and independently tested:

* `check_structural`  — types, required fields, closed enum membership, no floats.
* `check_semantic`    — one event against its predecessor: ordering, scope, taint.
* `check_cross_event` — a whole log: gapless seq, hash chain, vclock vs causal_parents.

The split matters. PRD §9.8 gives the layers different failure behaviour (structural and
referential always raise; semantic raises only in strict mode and otherwise emits a
`nondeterminism_warning`), which is impossible to implement if the layers are one function.

Every failure is a typed `ValidationError` carrying an `E-EVENT-NNN` code and a docs
anchor, per AGENTS.md §4. Codes are stable: an analyser, a CI gate or a bundle importer
may branch on them, so renumbering one is a breaking change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from agentdx.events.schema import (
    EVENT_FIELDS,
    EVENT_SCOPES,
    SCHEMA_VERSION,
    Event,
    EventScope,
    EventType,
    FieldSpec,
    FieldType,
    payload_fields,
)

_DOCS: Final = "docs/event-schema.md"


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A single validation failure, addressed to a field of a specific event.

    Guarantees: `code` is stable across releases and is the thing callers branch on;
    `message` is for humans and may be reworded. `seq` is None only for failures found
    before the event's seq could be trusted.
    """

    code: str
    message: str
    seq: int | None = None
    field: str | None = None

    @property
    def docs_url(self) -> str:
        """Return the anchor in the event-schema contract that explains this code."""
        return f"{_DOCS}#{self.code.lower()}"

    def __str__(self) -> str:
        """Return a one-line rendering with the code, location and docs anchor."""
        where = f" at seq={self.seq}" if self.seq is not None else ""
        field = f" field={self.field!r}" if self.field else ""
        return f"[{self.code}]{where}{field}: {self.message} ({self.docs_url})"


class EventValidationError(Exception):
    """Raised when an event or a log fails validation.

    Guarantees: `errors` is non-empty and ordered as discovered, so the first entry is the
    earliest problem in the log. A malformed event is a bug in the emitter, not data to be
    tolerated (PRD §9.8), so this is raised rather than logged.
    """

    def __init__(self, errors: Sequence[ValidationError]) -> None:
        """Build the exception from one or more validation errors."""
        self.errors: tuple[ValidationError, ...] = tuple(errors)
        super().__init__("; ".join(str(e) for e in self.errors))


# ---------------------------------------------------------------------------------------
# Layer (a) — structural
# ---------------------------------------------------------------------------------------


def _contains_float(value: object) -> bool:
    """Return True if a float appears anywhere in a payload value, at any depth.

    Takes `object`, not `PayloadValue`, on purpose. `PayloadValue` excludes float by
    construction, so a `PayloadValue`-typed parameter would make this function provably
    dead code — and it exists precisely for the inputs that never went through the type
    checker: a decoded bundle, a hand-edited log, an SDK caller without annotations.
    """
    if isinstance(value, float):
        return True
    if isinstance(value, Mapping):
        return any(_contains_float(v) for v in value.values())
    if isinstance(value, str):
        return False
    if isinstance(value, Sequence):
        return any(_contains_float(v) for v in value)
    return False


def _type_ok(value: object, spec: FieldSpec) -> bool:
    """Return True if `value` matches `spec.type`. `bool` never satisfies INT."""
    match spec.type:
        case FieldType.INT:
            return isinstance(value, int) and not isinstance(value, bool)
        case FieldType.STR:
            return isinstance(value, str)
        case FieldType.BOOL:
            return isinstance(value, bool)
        case FieldType.INT_ARRAY:
            return (
                isinstance(value, Sequence)
                and not isinstance(value, str)
                and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            )
        case FieldType.STR_ARRAY:
            return (
                isinstance(value, Sequence)
                and not isinstance(value, str)
                and all(isinstance(v, str) for v in value)
            )
        case FieldType.VCLOCK:
            return isinstance(value, Mapping) and all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for k, v in value.items()
            )
        case FieldType.OBJECT:
            return isinstance(value, Mapping) and all(isinstance(k, str) for k in value)


def _check_one_field(
    value: object, spec: FieldSpec, seq: int | None, path: str
) -> list[ValidationError]:
    """Validate a single present field against its spec. Returns [] when it conforms."""
    out: list[ValidationError] = []
    if value is None:
        if not spec.nullable:
            out.append(
                ValidationError("E-EVENT-004", f"{path} is not nullable but is null", seq, path)
            )
        return out
    if not _type_ok(value, spec):
        out.append(
            ValidationError(
                "E-EVENT-003",
                f"{path} must be {spec.type.value}, got {type(value).__name__}",
                seq,
                path,
            )
        )
        return out
    if spec.enum is not None and isinstance(value, str) and value not in spec.enum:
        allowed = ", ".join(sorted(spec.enum))
        out.append(
            ValidationError("E-EVENT-005", f"{path}={value!r} is not one of: {allowed}", seq, path)
        )
    if (
        spec.set_valued
        and isinstance(value, Sequence)
        and not isinstance(value, str)
        and list(value) != sorted(value)
    ):
        out.append(
            ValidationError(
                "E-EVENT-028",
                f"{path} is set-valued and must be emitted sorted; got {list(value)!r}. "
                f"The canonicaliser will not reorder it — silently sorting would hide a "
                f"nondeterministic emitter and surface much later as an intermittent G3 "
                f"failure. Use agentdx.sorted_set() at the emission site",
                seq,
                path,
            )
        )
    return out


def check_structural(event: Event) -> tuple[ValidationError, ...]:
    """Validate types, required fields and closed-enum membership (PRD §9.8, layer 1).

    Guarantees: every check is derived from `schema.EVENT_FIELDS` and
    `schema.PAYLOAD_SCHEMAS`; this function contains no field names of its own, so adding
    a field to the schema extends validation automatically. Never raises — returns the
    complete set of failures so a caller sees all of them at once.

    Failure modes: `E-EVENT-001` missing required field · `E-EVENT-002` unknown event type ·
    `E-EVENT-003` wrong type · `E-EVENT-004` null in a non-nullable field ·
    `E-EVENT-005` value outside a closed enum · `E-EVENT-006` unknown payload field ·
    `E-EVENT-008` schema_version mismatch · `E-EVENT-012` payload is not an object ·
    `E-EVENT-013` float in the event log ·
    `E-EVENT-028` a set-valued array was not emitted sorted.
    """
    out: list[ValidationError] = []
    declared_seq: object = event.seq
    seq = declared_seq if isinstance(declared_seq, int) else None

    # Deliberately re-widened to `object`. A dataclass does not enforce its annotations
    # at runtime, so an Event carrying a bogus `type` or a non-Mapping `payload` is
    # constructible — which is exactly what this layer exists to catch. Trusting the
    # annotation here would delete the check for the closed enum (E-EVENT-002).
    declared_type: object = event.type
    declared_payload: object = event.payload

    if not isinstance(declared_type, EventType):
        return (
            ValidationError(
                "E-EVENT-002",
                f"unknown event type {event.type!r}; the enum is closed (PRD §9.3)",
                seq,
                "type",
            ),
        )

    if event.schema_version != SCHEMA_VERSION:
        out.append(
            ValidationError(
                "E-EVENT-008",
                f"schema_version {event.schema_version} != {SCHEMA_VERSION}; migrate on read",
                seq,
                "schema_version",
            )
        )

    for spec in EVENT_FIELDS:
        if spec.name == "type":
            continue
        value = getattr(event, spec.name)
        if value is None and spec.required and not spec.nullable:
            out.append(ValidationError("E-EVENT-001", f"{spec.name} is required", seq, spec.name))
            continue
        out.extend(_check_one_field(value, spec, seq, spec.name))

    if not isinstance(declared_payload, Mapping):
        out.append(
            ValidationError(
                "E-EVENT-012",
                f"payload must be an object, got {type(event.payload).__name__}",
                seq,
                "payload",
            )
        )
        return tuple(out)

    specs = payload_fields(event.type)
    for name in event.payload:
        if name not in specs:
            out.append(
                ValidationError(
                    "E-EVENT-006",
                    f"payload.{name} is not in the {event.type.value} payload schema",
                    seq,
                    f"payload.{name}",
                )
            )
    for name, spec in specs.items():
        if name not in event.payload:
            if spec.required:
                out.append(
                    ValidationError(
                        "E-EVENT-001", f"payload.{name} is required", seq, f"payload.{name}"
                    )
                )
            continue
        out.extend(_check_one_field(event.payload[name], spec, seq, f"payload.{name}"))

    for name, value in event.payload.items():
        if _contains_float(value):
            out.append(
                ValidationError(
                    "E-EVENT-013",
                    f"payload.{name} contains a float; floats are forbidden in the event "
                    f"log (ruling R4) — use integer milliseconds or per-mille",
                    seq,
                    f"payload.{name}",
                )
            )

    return tuple(out)


# ---------------------------------------------------------------------------------------
# Layer (b) — semantic
# ---------------------------------------------------------------------------------------


def check_semantic(event: Event, previous: Event | None) -> tuple[ValidationError, ...]:
    """Validate one event against its immediate predecessor (PRD §9.6, §9.8, layer 2).

    `previous` is the event with `seq == event.seq - 1`, or None for the first event.

    Guarantees: checks only what is decidable from two adjacent events, so it is cheap
    enough to run on every write. Log-wide properties belong to `check_cross_event`.
    Never raises.

    Failure modes: `E-EVENT-020` a causal parent is not < seq · `E-EVENT-021` virtual_ts_ms
    decreased · `E-EVENT-022` seq is not gapless from 0 · `E-EVENT-023` span_id missing on a
    span-scoped type · `E-EVENT-024` span_id present on a run-scoped type ·
    `E-EVENT-025` duplicate entry in causal_parents · `E-EVENT-026` negative counter ·
    `E-EVENT-027` vclock regressed for this event's own slot.
    """
    out: list[ValidationError] = []
    seq = event.seq

    expected = 0 if previous is None else previous.seq + 1
    if seq != expected:
        out.append(
            ValidationError(
                "E-EVENT-022",
                f"seq must be gapless from 0: expected {expected}, got {seq}",
                seq,
                "seq",
            )
        )

    for parent in event.causal_parents:
        if parent >= seq:
            out.append(
                ValidationError(
                    "E-EVENT-020",
                    f"causal parent {parent} is not < seq {seq}; the log must be "
                    f"topologically sorted by construction (PRD §9.6)",
                    seq,
                    "causal_parents",
                )
            )
    if len(set(event.causal_parents)) != len(event.causal_parents):
        out.append(
            ValidationError(
                "E-EVENT-025", "causal_parents contains a duplicate", seq, "causal_parents"
            )
        )

    if previous is not None and event.virtual_ts_ms < previous.virtual_ts_ms:
        out.append(
            ValidationError(
                "E-EVENT-021",
                f"virtual_ts_ms decreased: {previous.virtual_ts_ms} -> {event.virtual_ts_ms}",
                seq,
                "virtual_ts_ms",
            )
        )

    if seq < 0 or event.sched_step < 0 or event.virtual_ts_ms < 0 or event.wall_ts_ms < 0:
        out.append(ValidationError("E-EVENT-026", "seq/step/timestamps must be non-negative", seq))

    scope = EVENT_SCOPES[event.type]
    if scope is EventScope.SPAN and event.span_id is None:
        out.append(
            ValidationError(
                "E-EVENT-023",
                f"{event.type.value} is span-scoped and requires span_id (PRD §9.3)",
                seq,
                "span_id",
            )
        )
    if scope is EventScope.RUN and event.span_id is not None:
        out.append(
            ValidationError(
                "E-EVENT-024",
                f"{event.type.value} is run-scoped and must not carry span_id",
                seq,
                "span_id",
            )
        )

    slot = event.clock_slot or event.agent_id
    if (
        previous is not None
        and slot is not None
        and (previous.clock_slot or previous.agent_id) == slot
        and event.vclock.get(slot, 0) < previous.vclock.get(slot, 0)
    ):
        out.append(
            ValidationError(
                "E-EVENT-027",
                f"vclock regressed for slot {slot!r}: "
                f"{previous.vclock.get(slot, 0)} -> {event.vclock.get(slot, 0)}",
                seq,
                "vclock",
            )
        )

    return tuple(out)


# ---------------------------------------------------------------------------------------
# Layer (c) — cross-event
# ---------------------------------------------------------------------------------------


def check_cross_event(events: Sequence[Event]) -> tuple[ValidationError, ...]:
    """Validate whole-log invariants that no pair of adjacent events can express.

    Guarantees: a single forward pass, relying on the fact that `causal_parents` entries are
    always `< seq` so every parent has been seen by the time its child is processed.
    Never raises.

    Failure modes: `E-EVENT-040` causal parent refers to a seq not in the log ·
    `E-EVENT-041` a causal parent's vclock is ahead of its child's in some slot ·
    `E-EVENT-042` fault taint was not inherited from a tainted causal parent (PRD §9.4
    rule 2) · `E-EVENT-043` more than one run_id in a single log ·
    `E-EVENT-044` the taint names a later fault than one reaching this event ·
    `E-EVENT-045` the taint names a fault never injected in this log.

    Not checkable here, and deliberately not faked: PRD §9.4 rule 3 taints an agent through
    its *context* after it consumes a faulted input. That edge is not recorded in the log,
    so an event carrying a `fault_id` with no tainted parent is accepted rather than
    reported. Documented in `docs/event-schema.md` §8.
    """
    out: list[ValidationError] = []
    by_seq: dict[int, Event] = {}
    injected_at: dict[str, int] = {}

    for event in events:
        if event.run_id and by_seq and event.run_id != next(iter(by_seq.values())).run_id:
            out.append(
                ValidationError(
                    "E-EVENT-043",
                    f"log contains more than one run_id ({event.run_id!r})",
                    event.seq,
                    "run_id",
                )
            )
        for parent_seq in event.causal_parents:
            parent = by_seq.get(parent_seq)
            if parent is None:
                out.append(
                    ValidationError(
                        "E-EVENT-040",
                        f"causal parent {parent_seq} is not present in the log",
                        event.seq,
                        "causal_parents",
                    )
                )
                continue
            for slot, count in parent.vclock.items():
                if event.vclock.get(slot, 0) < count:
                    out.append(
                        ValidationError(
                            "E-EVENT-041",
                            f"causal parent {parent_seq} has vclock[{slot!r}]={count} > "
                            f"{event.vclock.get(slot, 0)} on its child; a parent cannot be "
                            f"ahead of its child (PRD §14.2)",
                            event.seq,
                            "vclock",
                        )
                    )
            if parent.fault_id is not None and event.fault_id is None:
                out.append(
                    ValidationError(
                        "E-EVENT-042",
                        f"fault taint {parent.fault_id!r} from causal parent {parent_seq} "
                        f"was not inherited (PRD §9.4 rule 2)",
                        event.seq,
                        "fault_id",
                    )
                )
        by_seq[event.seq] = event
        out.extend(_check_taint_value(event, by_seq, injected_at))

    return tuple(out)


def _check_taint_value(
    event: Event, by_seq: Mapping[int, Event], injected_at: dict[str, int]
) -> list[ValidationError]:
    """Check that `fault_id` names the *right* fault, not merely some fault (PRD §9.4).

    `E-EVENT-042` only asks whether taint was inherited at all. That leaves the far more
    damaging bug untouched: inheriting the wrong fault. The taint is then present and
    plausible, every log validates, and the §2.6 cascade tree attributes effects to a fault
    that did not cause them — which is the whole reason the field exists.

    Two rules, both sound (they cannot fire on a correct log, so I5's precision discipline
    is preserved):

    * `E-EVENT-045` — the `fault_id` was never injected anywhere earlier in this log.
    * `E-EVENT-044` — the event descends from an *earlier* fault than the one it claims.
      PRD §9.4: "Where multiple faults contribute, `fault_id` holds the earliest."

    `fault_injected` and `fault_effect` are exempt from `E-EVENT-044`: PRD §9.4 rule 1 (the
    fault that *directly produced* this event) outranks rule 2 (inheritance), so an effect of
    a later fault legitimately carries that later fault's id even while descending from an
    earlier one.

    Mutates `injected_at`, recording each `fault_injected` before the event is checked, so a
    fault is known to its own injection event.
    """
    out: list[ValidationError] = []

    if event.type is EventType.FAULT_INJECTED and event.fault_id is not None:
        injected_at.setdefault(event.fault_id, event.seq)

    if event.fault_id is None:
        return out

    if event.fault_id not in injected_at:
        out.append(
            ValidationError(
                "E-EVENT-045",
                f"fault_id {event.fault_id!r} was never injected earlier in this log; taint "
                f"must name a fault the log accounts for (PRD §9.4)",
                event.seq,
                "fault_id",
            )
        )
        return out

    if event.type in (EventType.FAULT_INJECTED, EventType.FAULT_EFFECT):
        return out

    inherited = [
        injected_at[parent.fault_id]
        for parent_seq in event.causal_parents
        if (parent := by_seq.get(parent_seq)) is not None
        and parent.fault_id is not None
        and parent.fault_id in injected_at
    ]
    if inherited and injected_at[event.fault_id] > min(inherited):
        earliest = next(f for f, s in injected_at.items() if s == min(inherited))
        out.append(
            ValidationError(
                "E-EVENT-044",
                f"fault_id {event.fault_id!r} (injected at seq {injected_at[event.fault_id]}) "
                f"is later than {earliest!r} (injected at seq {min(inherited)}), which reaches "
                f"this event through causal_parents; PRD §9.4 requires the earliest",
                event.seq,
                "fault_id",
            )
        )
    return out


# ---------------------------------------------------------------------------------------
# Composed entry points
# ---------------------------------------------------------------------------------------


def validate_event(event: Event, previous: Event | None = None) -> None:
    """Run the structural and semantic layers on one event; raise on any failure.

    This is step 3 of PRD §9.6, called by the runtime between stamping and enqueueing.

    Guarantees: returns None iff the event is structurally and semantically valid.
    Semantic checks are skipped when structural checks already failed, because a
    wrongly-typed field makes every semantic answer meaningless noise.

    Raises:
        EventValidationError: with the complete error list.
    """
    errors = check_structural(event)
    if not errors:
        errors = check_semantic(event, previous)
    if errors:
        raise EventValidationError(errors)


def validate_log(events: Iterable[Event]) -> None:
    """Run all three layers over a complete log; raise on any failure.

    Guarantees: equivalent to calling `validate_event` on each event in order and then
    `check_cross_event` on the whole sequence. Used on seal and on bundle import, where
    the whole log is available and untrusted (PRD §31).

    Raises:
        EventValidationError: with the complete error list, earliest first.
    """
    materialised = list(events)
    errors: list[ValidationError] = []
    previous: Event | None = None
    for event in materialised:
        structural = check_structural(event)
        errors.extend(structural)
        if not structural:
            errors.extend(check_semantic(event, previous))
        previous = event
    if not errors:
        errors.extend(check_cross_event(materialised))
    if errors:
        raise EventValidationError(errors)
