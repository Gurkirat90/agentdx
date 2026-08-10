"""The definition-of-done property: the canonical hash sees stable fields and nothing else.

Both directions, every field, parametrised *from the schema marks* — so the test cannot
fall out of sync with the contract. Adding a field to `schema.py` adds cases here on the
next run; adding one without a mark is impossible, because `FieldSpec.volatility` has no
default.

This is the test that makes invariant I1 mean something. If it passes in only one
direction the project has a gate that either can never go green (a volatile field leaked
into the projection) or that goes green while the system is nondeterministic (a meaningful
field was excluded). The second is the dangerous one, which is why the "stable fields MUST
change the hash" half is not optional.
"""

from __future__ import annotations

import dataclasses

import pytest

from agentdx.events.canonical import canonical_log_hash
from agentdx.events.schema import PAYLOAD_SCHEMAS, EventType, FieldSpec
from tests.unit.events import factories


def _payload_cases() -> list[tuple[EventType, FieldSpec]]:
    """Return every (type, payload field) pair in the contract."""
    return [(t, spec) for t, specs in PAYLOAD_SCHEMAS.items() for spec in specs]


PAYLOAD_CASES = _payload_cases()
PAYLOAD_IDS = [
    f"{t.value}.payload.{spec.name}[{spec.volatility.value}]" for t, spec in PAYLOAD_CASES
]

TOP_LEVEL_CASES = factories.top_level_field_specs()
TOP_LEVEL_IDS = [f"{spec.name}[{spec.volatility.value}]" for spec in TOP_LEVEL_CASES]


@pytest.mark.parametrize("spec", TOP_LEVEL_CASES, ids=TOP_LEVEL_IDS)
def test_top_level_field_affects_hash_iff_stable(spec: FieldSpec) -> None:
    """A top-level field changes the canonical log hash exactly when it is marked STABLE."""
    log = factories.make_log()
    baseline = canonical_log_hash(log)

    mutated = [factories.mutate(log[2], spec, in_payload=False), *log[3:]]
    changed_log = [*log[:2], *mutated]
    assert changed_log[2] != log[2], "mutation produced an identical event"

    after = canonical_log_hash(changed_log)
    if spec.volatility.in_canonical:
        assert after != baseline, (
            f"{spec.name} is marked {spec.volatility.value} and MUST participate in "
            f"determinism equality, but mutating it left the hash unchanged"
        )
    else:
        assert after == baseline, (
            f"{spec.name} is marked {spec.volatility.value} and MUST NOT participate in "
            f"determinism equality, but mutating it changed the hash — gate G3 could "
            f"never pass"
        )


@pytest.mark.parametrize(("event_type", "spec"), PAYLOAD_CASES, ids=PAYLOAD_IDS)
def test_payload_field_affects_hash_iff_stable(event_type: EventType, spec: FieldSpec) -> None:
    """A payload field changes the canonical log hash exactly when it is marked STABLE."""
    event = factories.make_event(event_type, seq=0)
    payload = dict(event.payload)
    payload.setdefault(spec.name, factories.sample_value(spec, 0))
    event = factories.make_event(event_type, seq=0, payload=payload)

    baseline = canonical_log_hash([event])
    mutated = factories.mutate(event, spec, in_payload=True)
    assert mutated.payload[spec.name] != event.payload[spec.name]

    after = canonical_log_hash([mutated])
    if spec.volatility.in_canonical:
        assert after != baseline, (
            f"{event_type.value}.payload.{spec.name} is marked {spec.volatility.value} "
            f"and MUST participate in determinism equality"
        )
    else:
        assert after == baseline, (
            f"{event_type.value}.payload.{spec.name} is marked {spec.volatility.value} "
            f"and MUST NOT participate in determinism equality"
        )


def test_adding_a_zero_vclock_slot_is_invisible() -> None:
    """Sparse vclocks: an omitted slot and an explicit zero are the same clock (PRD §14.2).

    Without normalisation two semantically identical logs hash differently purely because
    of which agents existed when each event was stamped.
    """
    log = factories.make_log()
    padded = [dataclasses.replace(e, vclock={**e.vclock, "late_agent": 0}) for e in log]
    assert canonical_log_hash(padded) == canonical_log_hash(log)


def test_vclock_key_order_is_invisible() -> None:
    """Two clocks with the same slots in a different insertion order hash identically."""
    log = factories.make_log()
    forward = [dataclasses.replace(e, vclock={"a": 1, "b": 2, **e.vclock}) for e in log]
    backward = [dataclasses.replace(e, vclock={"b": 2, "a": 1, **e.vclock}) for e in log]
    assert canonical_log_hash(forward) == canonical_log_hash(backward)


def test_event_order_is_visible() -> None:
    """Reordering two events changes the hash — the log is a sequence, not a set."""
    log = factories.make_log()
    swapped = [*log[:2], log[3], log[2], *log[4:]]
    assert canonical_log_hash(swapped) != canonical_log_hash(log)


def test_every_field_carries_an_explicit_mark() -> None:
    """No field anywhere in the contract may be unmarked, and every type has a payload schema.

    `FieldSpec.volatility` is a required positional argument, so this is belt-and-braces —
    but it also asserts the totality of PAYLOAD_SCHEMAS over the closed enum, which
    `schema.payload_fields` relies on.
    """
    assert set(PAYLOAD_SCHEMAS) == set(EventType), "every event type needs a payload schema"
    for specs in PAYLOAD_SCHEMAS.values():
        for spec in specs:
            assert spec.volatility is not None
