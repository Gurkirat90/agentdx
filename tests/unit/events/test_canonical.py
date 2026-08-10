"""The exact bytes: key order, escaping, unicode form, number format, hash chain.

These are the tests another language's implementer would run against their port. Every
assertion here is a literal byte string, not a round-trip through our own encoder, because
a round-trip cannot catch "both sides are wrong in the same way".
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from agentdx.events.canonical import (
    CHAIN_GENESIS,
    FloatNotPermittedError,
    MalformedEventLineError,
    UnsupportedValueError,
    build_chain,
    canonical_bytes,
    canonical_log_hash,
    canonical_projection,
    chain_hash,
    decode_event,
    encode_event,
    encode_string,
    encode_value,
    normalise_vclock,
    verify_chain,
)
from agentdx.events.schema import EventType
from tests.unit.events import factories


class TestEncodeValue:
    """Byte-level rules for every permitted value shape."""

    def test_integers_have_no_sign_padding_or_exponent(self) -> None:
        """Integers have no sign padding or exponent."""
        assert encode_value(0) == "0"
        assert encode_value(1043) == "1043"
        assert encode_value(-5) == "-5"

    def test_booleans_and_null_are_json_literals(self) -> None:
        """Booleans and null are json literals."""
        assert encode_value(True) == "true"
        assert encode_value(False) == "false"
        assert encode_value(None) == "null"

    def test_bool_is_not_encoded_as_an_integer(self) -> None:
        """`bool` subclasses `int` in Python; the encoder must check it first."""
        assert encode_value(True) != "1"

    def test_object_keys_are_sorted_by_code_point(self) -> None:
        """Object keys are sorted by code point."""
        assert encode_value({"b": 1, "a": 2, "A": 3}) == '{"A":3,"a":2,"b":1}'

    def test_no_insignificant_whitespace(self) -> None:
        """No insignificant whitespace."""
        assert encode_value({"a": [1, 2]}) == '{"a":[1,2]}'

    def test_array_order_is_preserved_not_sorted(self) -> None:
        """Set-valued fields are the emitter's job to sort; the encoder never reorders."""
        assert encode_value(["b", "a"]) == '["b","a"]'

    def test_nested_objects_sort_at_every_level(self) -> None:
        """Nested objects sort at every level."""
        assert encode_value({"z": {"b": 1, "a": 2}}) == '{"z":{"a":2,"b":1}}'

    def test_float_is_rejected(self) -> None:
        """Float is rejected."""
        with pytest.raises(FloatNotPermittedError) as excinfo:
            encode_value({"factor": 1.5})
        assert "E-EVENT-013" in str(excinfo.value)

    def test_unsupported_type_is_rejected(self) -> None:
        """Unsupported type is rejected."""
        with pytest.raises(UnsupportedValueError) as excinfo:
            encode_value({"when": object()})
        assert "E-EVENT-014" in str(excinfo.value)


class TestEncodeString:
    """Escaping and unicode normalisation."""

    def test_only_required_characters_are_escaped(self) -> None:
        """Only required characters are escaped."""
        assert encode_string('a"b\\c') == '"a\\"b\\\\c"'

    def test_control_characters_use_lowercase_four_digit_hex(self) -> None:
        """Control characters use lowercase four digit hex."""
        assert encode_string("\x01") == '"\\u0001"'

    def test_short_escapes_are_preferred_for_the_named_controls(self) -> None:
        """Short escapes are preferred for the named controls."""
        assert encode_string("\n\t") == '"\\n\\t"'

    def test_non_ascii_is_emitted_literally(self) -> None:
        """`ensure_ascii` behaviour is a library choice; the contract fixes it to literal."""
        assert encode_string("café") == '"café"'
        assert encode_string("→").encode("utf-8") == '"→"'.encode()

    def test_nfd_and_nfc_normalise_to_the_same_bytes(self) -> None:
        """MacOS hands back NFD, Linux NFC. Both are supported platforms (CONTEXT.md §3)."""
        nfc = "caf\u00e9"
        nfd = "cafe\u0301"
        assert nfc != nfd
        assert encode_string(nfc) == encode_string(nfd)


class TestNormaliseVclock:
    """Sparse vector-clock canonicalisation (PRD §14.2)."""

    def test_zero_entries_are_dropped(self) -> None:
        """Zero entries are dropped."""
        assert normalise_vclock({"a": 1, "b": 0}) == {"a": 1}

    def test_keys_are_sorted(self) -> None:
        """Keys are sorted."""
        assert list(normalise_vclock({"z": 1, "a": 2})) == ["a", "z"]

    def test_an_all_zero_clock_becomes_empty(self) -> None:
        """An all zero clock becomes empty."""
        assert normalise_vclock({"a": 0, "b": 0}) == {}


class TestProjection:
    """Membership of the canonical projection, derived from the marks."""

    def test_volatile_top_level_fields_are_absent(self) -> None:
        """Volatile top level fields are absent."""
        projection = canonical_projection(factories.make_event())
        assert "wall_ts_ms" not in projection
        assert "run_id" not in projection

    def test_stable_top_level_fields_are_present(self) -> None:
        """Stable top level fields are present."""
        projection = canonical_projection(factories.make_event())
        for name in ("seq", "sched_step", "virtual_ts_ms", "vclock", "type", "causal_parents"):
            assert name in projection

    def test_volatile_payload_subfields_are_absent(self) -> None:
        """Volatile payload subfields are absent."""
        event = factories.make_event(EventType.SPAN_END)
        payload = canonical_projection(event)["payload"]
        assert isinstance(payload, dict)
        assert "duration_wall_ms" not in payload
        assert "duration_virtual_ms" in payload

    def test_run_end_wall_makespan_is_absent(self) -> None:
        """The gap found in PRD §10.7's 'exhaustive' exclusion list (ruling R3)."""
        payload = canonical_projection(factories.make_event(EventType.RUN_END))["payload"]
        assert isinstance(payload, dict)
        assert "wall_makespan_ms" not in payload
        assert "virtual_makespan_ms" in payload

    def test_cache_key_is_present(self) -> None:
        """Ruling R2: §11.4 guarantees portability, so the key is compared."""
        payload = canonical_projection(factories.make_event(EventType.LLM_CALL))["payload"]
        assert isinstance(payload, dict)
        assert "cache_key" in payload
        assert "perturbed_from_run" not in payload

    def test_canonical_bytes_are_utf8_with_no_trailing_newline(self) -> None:
        """Canonical bytes are utf8 with no trailing newline."""
        raw = canonical_bytes(factories.make_event())
        assert raw.decode("utf-8")
        assert not raw.endswith(b"\n")


class TestLogHash:
    """`canonical_log_hash` shape and sensitivity."""

    def test_hash_carries_the_algorithm_prefix(self) -> None:
        """Hash carries the algorithm prefix."""
        digest = canonical_log_hash(factories.make_log())
        assert digest.startswith("blake2b:")
        assert len(digest) == len("blake2b:") + 64

    def test_empty_log_has_a_stable_hash(self) -> None:
        """Empty log has a stable hash."""
        assert canonical_log_hash([]) == canonical_log_hash([])

    def test_hash_is_stable_across_calls(self) -> None:
        """Hash is stable across calls."""
        log = factories.make_log()
        assert canonical_log_hash(log) == canonical_log_hash(log)


class TestChain:
    """The append-only hash chain (PRD §9.7)."""

    def test_first_event_chains_from_genesis(self) -> None:
        """First event chains from genesis."""
        log = factories.make_log()
        chain = build_chain(log)
        assert chain[0][0] == CHAIN_GENESIS

    def test_each_link_uses_the_previous_hash(self) -> None:
        """Each link uses the previous hash."""
        chain = build_chain(factories.make_log())
        for (_, this_hash), (next_prev, _) in itertools.pairwise(chain):
            assert this_hash == next_prev

    def test_chain_covers_the_canonical_projection_only(self) -> None:
        """Mutating a volatile field must not change the chain, or bundles are unverifiable."""
        log = factories.make_log()
        volatile = [dataclasses.replace(e, wall_ts_ms=e.wall_ts_ms + 9999) for e in log]
        assert build_chain(volatile) == build_chain(log)

    def test_a_tampered_event_is_detected_at_its_seq(self) -> None:
        """A tampered event is detected at its seq."""
        log = factories.make_log()
        chain = build_chain(log)
        tampered = [*log[:3], dataclasses.replace(log[3], virtual_ts_ms=999999), *log[4:]]
        assert verify_chain(tampered, chain) == tampered[3].seq

    def test_an_untampered_log_verifies(self) -> None:
        """An untampered log verifies."""
        log = factories.make_log()
        assert verify_chain(log, build_chain(log)) is None

    def test_a_removed_event_is_detected(self) -> None:
        """A removed event is detected."""
        log = factories.make_log()
        chain = build_chain(log)
        assert verify_chain([*log[:3], *log[4:]], chain) is not None

    def test_chain_hash_is_deterministic(self) -> None:
        """Chain hash is deterministic."""
        event = factories.make_event()
        assert chain_hash(CHAIN_GENESIS, event) == chain_hash(CHAIN_GENESIS, event)


class TestSerialisationRoundTrip:
    """Full-fidelity encode/decode used by bundles (PRD §20.7)."""

    def test_decode_reverses_encode_for_every_event_type(self) -> None:
        """Decode reverses encode for every event type."""
        for event_type in EventType:
            event = factories.make_event(event_type)
            assert canonical_bytes(decode_event(encode_event(event))) == canonical_bytes(event)

    def test_encoded_event_is_one_line(self) -> None:
        """Encoded event is one line."""
        assert "\n" not in encode_event(factories.make_event())

    def test_encoded_event_retains_volatile_fields(self) -> None:
        """The storage form is not the projection: PRD §9.2 requires wall_ts_ms to exist."""
        assert '"wall_ts_ms"' in encode_event(factories.make_event())

    def test_decoding_a_float_is_rejected(self) -> None:
        """Decoding a float is rejected."""
        line = encode_event(factories.make_event()).replace('"wall_ts_ms":0', '"wall_ts_ms":0.5')
        with pytest.raises(FloatNotPermittedError):
            decode_event(line)

    def test_decoding_a_non_object_line_is_rejected(self) -> None:
        """Decoding a non object line is rejected."""
        with pytest.raises(MalformedEventLineError):
            decode_event("[1,2,3]")
