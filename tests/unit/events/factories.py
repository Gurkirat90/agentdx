"""Event builders for the P02 suite, generated from the schema marks rather than hand-typed.

Everything here derives from `schema.EVENT_FIELDS` and `schema.PAYLOAD_SCHEMAS`. That is
the point: if a field is added to the contract and this file needed editing to keep the
tests compiling, the volatility property test could silently stop covering it. Instead a
new field is picked up automatically and the property test's parametrisation grows.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from agentdx.events.schema import (
    EVENT_FIELDS,
    EVENT_SCOPES,
    PAYLOAD_SCHEMAS,
    SCHEMA_VERSION,
    Event,
    EventScope,
    EventType,
    FieldSpec,
    FieldType,
    PayloadValue,
)

RUN_ID = "r_f2a91"
_NO_DISTINCT_VALUE = "no distinct value could be generated for {name}"


def sample_value(spec: FieldSpec, salt: int = 0) -> PayloadValue:
    """Return a deterministic, schema-conforming value for `spec`.

    Distinct `salt` values give distinct results for every field type, which is what makes
    `mutate` able to produce a genuinely different value for any field in the contract.
    """
    if spec.enum is not None:
        options = sorted(spec.enum)
        return options[salt % len(options)]
    match spec.type:
        case FieldType.INT:
            return 100 + salt
        case FieldType.STR:
            return f"{spec.name}-{salt}"
        case FieldType.BOOL:
            return salt % 2 == 1
        case FieldType.INT_ARRAY:
            return [salt]
        case FieldType.STR_ARRAY:
            return [f"{spec.name}-{salt}"]
        case FieldType.VCLOCK:
            return {"planner": salt + 1}
        case FieldType.OBJECT:
            return {f"k{salt}": f"v{salt}"}


def sample_payload(event_type: EventType, salt: int = 0) -> dict[str, PayloadValue]:
    """Return a complete, schema-conforming payload for `event_type`.

    Includes every required field and omits every optional one, so a payload built here is
    the minimal valid payload — the shape most likely to expose a missing-field bug.
    """
    return {
        spec.name: sample_value(spec, salt) for spec in PAYLOAD_SCHEMAS[event_type] if spec.required
    }


def make_event(
    event_type: EventType = EventType.STATE_WRITE,
    *,
    seq: int = 0,
    salt: int = 0,
    causal_parents: Sequence[int] | None = None,
    vclock: Mapping[str, int] | None = None,
    agent_id: str | None = "coder",
    fault_id: str | None = None,
    payload: Mapping[str, PayloadValue] | None = None,
) -> Event:
    """Return a valid `Event` of the given type, with span_id set iff the scope requires it."""
    scope = EVENT_SCOPES[event_type]
    span_id = None if scope is EventScope.RUN else "a3f19c22b0d1"
    return Event(
        schema_version=SCHEMA_VERSION,
        run_id=RUN_ID,
        seq=seq,
        sched_step=seq,
        virtual_ts_ms=seq * 10,
        wall_ts_ms=seq,
        vclock=dict(vclock) if vclock is not None else {"coder": seq + 1},
        type=event_type,
        causal_parents=list(causal_parents) if causal_parents is not None else [],
        payload=dict(payload) if payload is not None else sample_payload(event_type, salt),
        agent_id=None if scope is EventScope.RUN else agent_id,
        clock_slot=None if scope is EventScope.RUN else agent_id,
        span_id=span_id,
        fault_id=fault_id,
    )


def make_log(length: int = 6) -> list[Event]:
    """Return a short valid log: run_start, a span's worth of work, run_end.

    Guarantees: gapless seq from 0, non-decreasing virtual_ts_ms, causal_parents strictly
    backwards, span_id present exactly where the scope table requires it. Passes
    `validators.validate_log`.
    """
    body_types = [
        EventType.SPAN_START,
        EventType.STATE_READ,
        EventType.LLM_CALL,
        EventType.STATE_WRITE,
        EventType.SPAN_END,
    ]
    events = [make_event(EventType.RUN_START, seq=0, vclock={})]
    seq = 1
    while seq < length - 1:
        event_type = body_types[(seq - 1) % len(body_types)]
        events.append(make_event(event_type, seq=seq, salt=seq, causal_parents=[seq - 1]))
        seq += 1
    events.append(
        make_event(EventType.RUN_END, seq=seq, causal_parents=[seq - 1], vclock={"coder": seq})
    )
    return events


def top_level_field_specs() -> list[FieldSpec]:
    """Return every top-level field spec except `type`, which mutates the payload schema."""
    return [spec for spec in EVENT_FIELDS if spec.name != "type"]


def mutate(event: Event, spec: FieldSpec, *, in_payload: bool) -> Event:
    """Return a copy of `event` with one field changed to a different conforming value.

    Guarantees: the returned event differs from the input in exactly that one field, and
    the new value is never equal to the old one — asserted by the caller. For `vclock` the
    mutation adds a *non-zero* slot, because adding a zero slot is by design invisible to
    the canonical form (sparse normalisation, PRD §14.2).
    """
    if in_payload:
        current = event.payload.get(spec.name)
        replacement = _different(spec, current)
        return dataclasses.replace(event, payload={**event.payload, spec.name: replacement})

    current = getattr(event, spec.name)
    if spec.name == "vclock":
        return dataclasses.replace(event, vclock={**event.vclock, "reviewer": 7})
    replacement = _different(spec, current)
    return dataclasses.replace(event, **{spec.name: replacement})


def _different(spec: FieldSpec, current: object) -> PayloadValue:
    """Return a conforming value for `spec` that is not equal to `current`."""
    for salt in range(8):
        candidate = sample_value(spec, salt)
        if candidate != current:
            return candidate
    raise AssertionError(_NO_DISTINCT_VALUE.format(name=spec.name))
