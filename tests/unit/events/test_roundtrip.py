"""Round-trip: serialise → deserialise → canonicalise is byte-identical, over 1 000 logs.

The property that matters for bundles (PRD §20.7): a log written on machine A, shipped as
`events.jsonl` and read on machine B must canonicalise to the same bytes, or `--verify`
compares two different things and reports a difference that is really an encoding artefact.

Hypothesis generates the awkward inputs deliberately — astral-plane characters, combining
marks, control characters, empty containers, deeply nested objects, keys that sort
differently under UTF-16 than under UTF-8.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentdx.events.canonical import (
    canonical_bytes,
    canonical_log_hash,
    decode_event,
    encode_event,
)
from agentdx.events.schema import SCHEMA_VERSION, Event, EventType, PayloadValue
from tests.unit.events import factories

# Deliberately nasty text: astral plane, combining marks, controls, quotes, backslashes.
TEXT = st.text(
    alphabet=st.characters(
        codec="utf-8",
        exclude_categories=("Cs",),
    ),
    max_size=24,
)

SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**40), max_value=2**40),
    TEXT,
)


def _payload_values() -> st.SearchStrategy[PayloadValue]:
    """Return a strategy for arbitrary permitted payload values, floats excluded by design."""
    return st.recursive(
        SCALARS,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(TEXT, children, max_size=4),
        ),
        max_leaves=8,
    )


@st.composite
def events(draw: st.DrawFn) -> Event:
    """Return a strategy for a single encodable event of an arbitrary type."""
    event_type = draw(st.sampled_from(list(EventType)))
    base = factories.make_event(event_type, seq=draw(st.integers(0, 10_000)))
    extra_key = draw(TEXT)
    payload: dict[str, PayloadValue] = dict(base.payload)
    if extra_key:
        payload[extra_key] = draw(_payload_values())
    return Event(
        schema_version=SCHEMA_VERSION,
        run_id=draw(TEXT) or "r_f2a91",
        seq=base.seq,
        sched_step=draw(st.integers(0, 10_000)),
        virtual_ts_ms=draw(st.integers(0, 10**9)),
        wall_ts_ms=draw(st.integers(0, 10**9)),
        vclock=draw(st.dictionaries(TEXT, st.integers(0, 500), max_size=4)),
        type=event_type,
        causal_parents=draw(st.lists(st.integers(0, 10_000), max_size=4)),
        payload=payload,
        agent_id=draw(st.one_of(st.none(), TEXT)),
        clock_slot=draw(st.one_of(st.none(), TEXT)),
        span_id=draw(st.one_of(st.none(), TEXT)),
        fault_id=draw(st.one_of(st.none(), TEXT)),
    )


@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(log=st.lists(events(), min_size=1, max_size=6))
def test_encode_decode_canonicalise_is_byte_identical(log: list[Event]) -> None:
    """1 000 generated logs: the canonical bytes survive a full serialisation round trip."""
    revived = [decode_event(encode_event(event)) for event in log]
    for original, restored in zip(log, revived, strict=True):
        assert canonical_bytes(restored) == canonical_bytes(original)
    assert canonical_log_hash(revived) == canonical_log_hash(log)


@settings(max_examples=200, deadline=None)
@given(event=events())
def test_encoding_is_idempotent(event: Event) -> None:
    """Encoding a decoded event reproduces the same line — the form is a fixed point."""
    once = encode_event(event)
    assert encode_event(decode_event(once)) == once


@settings(max_examples=200, deadline=None)
@given(event=events())
def test_an_encoded_event_is_a_single_jsonl_record(event: Event) -> None:
    """No raw newline may survive encoding, or a log stops being valid JSON Lines."""
    assert "\n" not in encode_event(event)
    assert "\r" not in encode_event(event)
