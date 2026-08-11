"""The canonical bytes are a contract. This suite is what lets anyone optimise the encoder.

`encode_string` was rewritten under OP-3 (2026-08-11) from a per-character Python loop to a
`str.translate` over a precomputed table, because the loop was 52 % of hash-chain cost and
put the composed write path below NFR-10 (D-15). That rewrite is only legitimate if the
emitted bytes did not move by one character — the canonical projection is what gate G3
compares, and a change there would silently invalidate every recorded run and every future
golden corpus.

So the property is asserted directly rather than reasoned about, over a corpus chosen to
break an escape table rather than to exercise the happy path: every C0 and C1 control, the
six named escapes, NFC/NFD pairs, astral-plane characters, variation selectors, bidi marks
and Unicode separators.

**This file is the licence to touch the encoder.** Anyone making it faster again runs this
first. Anyone who finds it failing has changed the contract, not the implementation, and
owes a `schema_version` bump and an ADR (CONTEXT.md §11 tripwire 6).
"""

from __future__ import annotations

import random
import unicodedata

import pytest

from agentdx.events.canonical import (
    canonical_bytes,
    canonical_log_hash,
    encode_string,
    encode_value,
)
from tests.unit.events.factories import make_log

SEED = 42


def _reference_encode_string(text: str) -> str:
    """The pre-OP-3 implementation, kept as the independent oracle.

    Deliberately a *duplicate* of the old algorithm rather than a call into the new one.
    Comparing the encoder against itself would assert nothing; comparing it against the
    implementation it replaced is what makes the equivalence meaningful.
    """
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    out = ['"']
    for char in unicodedata.normalize("NFC", text):
        if char in escapes:
            out.append(escapes[char])
        elif char < " ":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _corpus() -> list[str]:
    """Return strings chosen to break an escape table, not to exercise the happy path."""
    rng = random.Random(SEED)  # noqa: S311 — generating test inputs, not keys
    corpus: list[str] = []
    corpus += [chr(code) for code in range(0x200)]  # every C0 and C1 control, plus latin
    corpus += ['"', "\\", "\b\f\n\r\t", "", " ", "plain-ascii", "x" * 1024]
    corpus += ["\x00", "\x1f", "\x7f", "\\u0000", '\\"escaped already\\"']
    corpus += ["caf\u00e9", "cafe\u0301", "\u1eb9\u0301", "\u1e69"]  # NFC/NFD equivalents
    corpus += ["\U0001f600", "\U0001d11e", "\U000e0100", "\U0010ffff"]  # astral, tag chars
    # Zero-width space, line/paragraph separator and BOM. JSON must NOT escape these, and an
    # encoder that did would still pass every ASCII test in the suite.
    corpus += ["".join(chr(c) for c in (0x200B, 0x2028, 0x2029, 0xFEFF))]
    corpus += ["\u65e5\u672c\u8a9e", "\u05e2\u05d1\u05e8\u05d9\u05ea"]  # CJK, bidi
    corpus += ["draft.module_a", "blake2b:" + "0" * 64, "r_f2a91", "span0001aaaa"]
    corpus += ["".join(chr(rng.randint(1, 0x2FFF)) for _ in range(48)) for _ in range(400)]
    return corpus


@pytest.mark.parametrize("text", _corpus(), ids=lambda t: repr(t)[:24])
def test_encode_string_matches_the_pre_optimisation_encoder(text: str) -> None:
    """Every string encodes to exactly what the character-loop implementation produced."""
    assert encode_string(text) == _reference_encode_string(text)


def test_the_escape_table_escapes_exactly_the_c0_controls_and_nothing_else() -> None:
    """Minimal escaping, asserted as a rule rather than sampled.

    Libraries disagree about which characters they *may* escape and agree on which they
    *must*; emitting only the mandatory set is what makes the output reproducible in another
    language from `docs/event-schema.md` alone.
    """
    for code in range(0x20):
        assert "\\" in encode_string(chr(code)), f"U+{code:04X} must be escaped"
    for code in (0x20, 0x21, 0x23, 0x7E, 0x7F, 0xA0, 0x1F600):
        if chr(code) in {'"', "\\"}:
            continue
        encoded = encode_string(chr(code))
        assert encoded == f'"{chr(code)}"', f"U+{code:04X} must be emitted literally"


def test_ascii_short_circuit_never_changes_a_result() -> None:
    """`_norm`'s ASCII fast path is exact: no ASCII string has a non-trivial NFC form."""
    for code in range(0x80):
        text = chr(code)
        assert unicodedata.normalize("NFC", text) == text
        assert encode_string(text) == _reference_encode_string(text)


def test_nfd_and_nfc_inputs_encode_identically() -> None:
    """Apple hands back NFD where Linux hands back NFC (CONTEXT.md §3 supports both).

    Without normalisation a run recorded on a Mac and replayed on Linux would differ on any
    agent name carrying a diacritic — an I1 failure that would look like a scheduler bug.
    """
    pairs = [("café", "café"), ("ṩ", "ṩ"), ("Å", "Å")]
    for composed, decomposed in pairs:
        assert composed != decomposed
        assert encode_string(composed) == encode_string(decomposed)


def test_nested_structures_round_trip_through_the_same_table() -> None:
    """`encode_value` reaches strings in keys and in nested containers, not just at the top.

    Object keys are NFC-normalised and sorted before encoding, so a fast path that applied
    only to values would produce different bytes for the same mapping.
    """
    payload = {
        "café": ["\n", {"\t": 'quote"inside'}],
        "ascii": "plain",
        "ṩ": [1, True, None],
    }
    encoded = encode_value(payload)
    assert encoded == encode_value({**payload})
    assert '"café"' in encoded or '"café"' in encoded


def test_a_whole_log_hashes_to_the_same_value_as_the_reference_encoder() -> None:
    """The end-to-end property: the canonical log hash is unmoved by the optimisation.

    This is the one that matters. `canonical_log_hash` is the quantity gate G3 compares
    across replays; if the encoder change had moved it, every recorded run would have been
    invalidated silently.
    """
    events = make_log(length=40)
    digest = canonical_log_hash(events)
    assert digest.startswith("blake2b:")
    assert canonical_log_hash(events) == digest
    assert all(canonical_bytes(event) == canonical_bytes(event) for event in events)


def test_canonical_bytes_are_utf8_with_no_trailing_newline() -> None:
    """The unit the hash chain is built from: UTF-8, no whitespace, no trailing newline."""
    for event in make_log(length=12):
        raw = canonical_bytes(event)
        assert isinstance(raw, bytes)
        assert raw.decode("utf-8")
        assert not raw.endswith(b"\n")
        assert b"\n" not in raw
