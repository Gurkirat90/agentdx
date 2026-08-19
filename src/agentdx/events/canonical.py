"""The canonical projection, its exact bytes, and the append-only hash chain (PRD §10.7).

This module turns invariant I1 from a claim into a testable equality. Two executions are
"the same run" iff the byte string this module produces for their logs is identical.

Two rules govern everything here.

1. **Membership is derived, never listed.** Which fields survive the projection comes from
   `Volatility.in_canonical` in `schema.py` and from nowhere else. PRD §10.7 ships a
   hand-maintained exclusion list that calls itself exhaustive and is already missing
   `run_end.payload.wall_makespan_ms` — that is the drift this design exists to prevent.

2. **The bytes are fully specified, not delegated.** Serialisation is hand-written rather
   than handed to `json.dumps`, so that every decision (key order, escaping, unicode form,
   number format) is stated in one place and can be reimplemented in another language from
   `docs/event-schema.md` alone. A canonical form whose definition is "whatever this
   version of the standard library does" is not a contract.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from hashlib import blake2b
from typing import Final

from agentdx.events.schema import (
    EVENT_FIELDS,
    SCHEMA_VERSION,
    Event,
    EventType,
    PayloadValue,
    canonical_payload_field_names,
)

HASH_PREFIX: Final = "blake2b:"
DIGEST_SIZE: Final = 32

CHAIN_GENESIS: Final = HASH_PREFIX + "0" * 64
"""The `prev_hash` of the first event in a run. A fixed, explicit value rather than an
empty string, so that a truncated log cannot be mistaken for a complete one whose chain
happens to verify."""

_ESCAPES: Final[Mapping[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_DOCS: Final = "docs/event-schema.md"

_NOT_AN_OBJECT: Final = "an event line must decode to an object"
_PAYLOAD_NOT_OBJECT: Final = "payload must be an object"
_VCLOCK_NOT_OBJECT: Final = "vclock must be an object"
_PARENTS_NOT_ARRAY: Final = "causal_parents must be an array"


class FloatNotPermittedError(TypeError):
    """A float reached the serialiser. Floats are forbidden in the event log (ruling R4).

    Carries `E-EVENT-013`, the same code the structural validator uses, so a caller sees
    one code for the rule whichever layer catches the violation. This class is the backstop
    for the path that bypasses validation — a hand-edited or foreign log being decoded.
    """

    code: Final = "E-EVENT-013"

    def __init__(self, where: str) -> None:
        """Build the error from a description of where the float was found."""
        super().__init__(
            f"[{self.code}] {where} is a float; floats are forbidden in the event log "
            f"because cross-platform float formatting is a determinism leak the canonical "
            f"projection cannot normalise (ruling R4). Use integer milliseconds or "
            f"per-mille. ({_DOCS}#{self.code.lower()})"
        )


class UnsupportedValueError(TypeError):
    """A value outside `PayloadValue` reached the serialiser.

    Carries `E-EVENT-014`. Distinct from `FloatNotPermittedError` because a float is a
    policy decision with a documented workaround, whereas this is an emitter bug.
    """

    code: Final = "E-EVENT-014"

    def __init__(self, type_name: str, where: str) -> None:
        """Build the error from the offending type and the path at which it appeared."""
        super().__init__(
            f"[{self.code}] {where} has type {type_name}, which is not permitted in the "
            f"event log; values must be str, int, bool, null, array or object "
            f"({_DOCS}#{self.code.lower()})"
        )


class MalformedEventLineError(ValueError):
    """A stored event line does not have the shape the contract requires.

    Carries `E-EVENT-015`. Raised only on the decode path, which handles untrusted input
    (a bundle from another machine, PRD §31).
    """

    code: Final = "E-EVENT-015"

    def __init__(self, detail: str) -> None:
        """Build the error from a description of the structural problem."""
        super().__init__(f"[{self.code}] {detail} ({_DOCS}#{self.code.lower()})")


# ---------------------------------------------------------------------------------------
# Serialisation primitives — the exact bytes
# ---------------------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Return `text` in Unicode Normalisation Form C.

    Guarantees: two strings that are canonically equivalent under Unicode produce identical
    bytes. This is not theoretical — macOS filesystems hand back NFD where Linux hands back
    NFC, and CONTEXT.md §3 supports both, so without this a run recorded on a Mac and
    replayed on Linux could differ on an agent name containing a diacritic.

    The ASCII short-circuit is exact, not an approximation: every ASCII string is already in
    NFC, because no character below U+0080 has a canonical decomposition or a composition
    partner. It is a fast C-level check that skips a comparatively expensive call for the
    overwhelmingly common case (agent ids, event types, hex hashes, state keys).
    """
    return text if text.isascii() else unicodedata.normalize("NFC", text)


_TRANSLATION: Final[Mapping[int, str]] = {
    **{ord(char): escape for char, escape in _ESCAPES.items()},
    **{code: f"\\u{code:04x}" for code in range(0x20) if chr(code) not in _ESCAPES},
}
r"""The complete escape table, keyed by code point, built once at import.

Exactly the mapping the character loop this replaced applied: the six named escapes of
`_ESCAPES`, plus `\uXXXX` for every remaining C0 control. Nothing else is escaped, so the
emitted bytes are unchanged — asserted over an adversarial corpus by
`tests/unit/events/test_canonical_bytes_are_stable.py`, which is the test that makes this
optimisation safe to have made at all.
"""


def encode_string(text: str) -> str:
    r"""Return the canonical JSON encoding of a string, including its surrounding quotes.

    Guarantees: NFC-normalised; escapes exactly `"`, `\` and the C0 controls and nothing
    else. All other characters — including every non-ASCII character — are emitted
    literally and encoded as UTF-8 by `canonical_bytes`. Minimal escaping is what makes the
    output reproducible across languages, since libraries disagree about which characters
    they *may* escape but agree on which they *must*.

    **The fast path skips the per-character work rather than speeding it up.** Hex hashes,
    agent ids, event type names and state keys are the overwhelming majority of strings in an
    event, and not one of them contains a character that needs escaping — so four C-level
    scans decide that and the string is returned untouched. Only a string that actually
    contains an escapable character pays for `str.translate`; only a non-ASCII one pays for
    NFC normalisation.

    The bytes are identical to the per-character loop this replaced. That is the whole
    constraint on this function, and it is asserted directly against a duplicate of that loop
    in `tests/unit/events/test_canonical_bytes_are_stable.py` — never assumed.

    Measured on the target platform (macOS/arm64/CPython 3.12), composed write path:
    16 216 → 23 232 events/s, which is what returned NFR-10 to compliance (D-17).
    **A warning for whoever optimises this next:** the obvious intermediate — `str.translate`
    on *every* string — measured 1.29× faster on Linux/CPython 3.10 and *slower than the
    original loop* on 3.12, because a dict translation table costs a full dict lookup per
    character while the loop benefits from the specialising interpreter. Measure on the
    target platform or the number is not evidence.
    """
    if text.isascii():
        if text.isprintable() and '"' not in text and "\\" not in text:
            return '"' + text + '"'
        return '"' + text.translate(_TRANSLATION) + '"'
    return '"' + unicodedata.normalize("NFC", text).translate(_TRANSLATION) + '"'


def encode_value(value: PayloadValue, *, path: str = "$") -> str:
    """Return the canonical JSON encoding of any permitted value.

    Guarantees: integers are emitted in decimal with no sign for non-negatives, no leading
    zeros and no exponent — `str(int)` in Python, identical in every language. Objects have
    their keys NFC-normalised and then sorted by Unicode code point, which for valid
    Unicode is byte-for-byte the same order as sorting the UTF-8 encodings; this
    deliberately differs from RFC 8785, which sorts by UTF-16 code unit and therefore
    orders astral-plane keys differently. Array order is preserved, never sorted: a
    set-valued field must be emitted already sorted (see `docs/event-schema.md` §5).

    Args:
        value: Any value permitted by `PayloadValue`.
        path: Dotted location used in error messages only; never affects output.

    Raises:
        FloatNotPermittedError: a float appeared anywhere in `value` (`E-EVENT-013`).
        UnsupportedValueError: a value outside `PayloadValue` appeared (`E-EVENT-014`).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        raise FloatNotPermittedError(path)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return encode_string(value)
    if isinstance(value, Mapping):
        items = sorted((_norm(str(k)), v) for k, v in value.items())
        body = ",".join(
            f"{encode_string(k)}:{encode_value(v, path=f'{path}.{k}')}" for k, v in items
        )
        return "{" + body + "}"
    if isinstance(value, Sequence):
        body = ",".join(encode_value(v, path=f"{path}[{i}]") for i, v in enumerate(value))
        return "[" + body + "]"
    raise UnsupportedValueError(type(value).__name__, path)


def normalise_vclock(vclock: Mapping[str, int]) -> dict[str, int]:
    """Return a vector clock in canonical sparse form: zero entries dropped, keys sorted.

    Guarantees: `{"a": 1}` and `{"a": 1, "b": 0}` — the same clock under PRD §14.2, where an
    absent slot reads as 0 — produce the same result and therefore the same hash. Without
    this, two semantically identical logs hash differently for no reason other than which
    agents happened to have been created by the time the event was stamped.
    """
    return {_norm(k): v for k, v in sorted(vclock.items()) if v != 0}


# ---------------------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------------------


def canonical_projection(event: Event) -> dict[str, PayloadValue]:
    """Return the determinism-relevant subset of an event, as a plain mapping.

    Guarantees: contains exactly those fields whose `Volatility.in_canonical` is True,
    computed from `schema.py` at call time. Nothing in this function names a field.
    Assumes the event has passed `validators.check_structural`; unknown payload keys are
    dropped rather than encoded, since a validated event cannot have any.
    """
    out: dict[str, PayloadValue] = {}
    for spec in EVENT_FIELDS:
        if not spec.volatility.in_canonical:
            continue
        value = getattr(event, spec.name)
        if spec.name == "type":
            out[spec.name] = EventType(value).value
        elif spec.name == "vclock":
            out[spec.name] = normalise_vclock(value)
        elif spec.name == "causal_parents":
            out[spec.name] = list(value)
        else:
            out[spec.name] = value

    keep = canonical_payload_field_names(event.type)
    out["payload"] = {k: v for k, v in event.payload.items() if k in keep}
    return out


def canonical_bytes(event: Event) -> bytes:
    """Return the exact bytes that represent this event for determinism comparison.

    Guarantees: UTF-8, no trailing newline, no insignificant whitespace, keys sorted by
    Unicode code point at every level. Byte-identical for the same event on any machine,
    OS and Python build. This is the unit the hash chain and the log hash are built from.
    """
    return encode_value(canonical_projection(event)).encode("utf-8")


def canonical_log_hash(events: Iterable[Event]) -> str:
    """Return the single hash that gate G3 compares across replays (PRD §10.7).

    The construction is the PRD's, unchanged: one rolling blake2b-256 over each event's
    canonical bytes followed by a newline separator. The separator is what stops two
    adjacent events from being re-partitioned into a different pair with the same digest.

    Guarantees: order-sensitive, prefix-free per event, and independent of every field
    marked VOLATILE or IDENTITY. Returned with the `blake2b:` prefix used everywhere else
    in the system (PRD §20.5, §38).
    """
    digest = blake2b(digest_size=DIGEST_SIZE)
    for event in events:
        digest.update(canonical_bytes(event) + b"\n")
    return HASH_PREFIX + digest.hexdigest()


# ---------------------------------------------------------------------------------------
# The append-only hash chain (PRD §9.7)
# ---------------------------------------------------------------------------------------


def chain_hash(prev_hash: str, event: Event) -> str:
    """Return `this_hash` for an event given its predecessor's `this_hash`.

    The chain covers the *canonical projection only*. Chaining the volatile fields would
    give every run a unique chain, which would make tamper detection useless for exactly
    the case it exists for: verifying a bundle received from another machine (PRD §9.7,
    §31). A recipient can recompute this chain and compare it with the sender's.

    `prev_hash` and `this_hash` live beside the event — as `events` table columns, per the
    DDL in PRD §38 — and never inside it. An event whose canonical form contained its own
    hash would be self-referential, which is why PRD §9.2 lists neither field. Recorded as
    ruling C-4.

    Guarantees: deterministic; depends only on the predecessor hash and this event's
    canonical bytes. Use `CHAIN_GENESIS` as `prev_hash` for `seq == 0`.
    """
    digest = blake2b(digest_size=DIGEST_SIZE)
    digest.update(prev_hash.encode("utf-8"))
    digest.update(b"\n")
    digest.update(canonical_bytes(event))
    return HASH_PREFIX + digest.hexdigest()


def build_chain(events: Iterable[Event]) -> tuple[tuple[str, str], ...]:
    """Return `(prev_hash, this_hash)` for each event, in order.

    Guarantees: the first pair's `prev_hash` is `CHAIN_GENESIS`; each subsequent
    `prev_hash` is the preceding `this_hash`.
    """
    out: list[tuple[str, str]] = []
    prev = CHAIN_GENESIS
    for event in events:
        current = chain_hash(prev, event)
        out.append((prev, current))
        prev = current
    return tuple(out)


def verify_chain(events: Sequence[Event], hashes: Sequence[tuple[str, str]]) -> int | None:
    """Return the seq of the first event whose chain entry does not verify, or None.

    Guarantees: returns None iff the stored chain matches a freshly computed one over the
    same events, which means no event was altered, inserted, removed or reordered.
    Returns the *first* divergence so the caller can report where tampering begins rather
    than that it happened.
    """
    if len(events) != len(hashes):
        return events[min(len(hashes), len(events) - 1)].seq if events else None
    for event, (prev, stored), (expected_prev, expected) in zip(
        events, hashes, build_chain(events), strict=True
    ):
        if prev != expected_prev or stored != expected:
            return event.seq
    return None


# ---------------------------------------------------------------------------------------
# Full-fidelity serialisation (bundles: events.jsonl — PRD §20.7)
# ---------------------------------------------------------------------------------------


def encode_event(event: Event) -> str:
    """Return the full event as one canonical JSON line, volatile fields included.

    This is the *storage* form, not the projection: `wall_ts_ms` and friends are recorded,
    because PRD §9.2 requires them to exist for overhead accounting. It uses the same byte
    rules as the projection so that `encode → decode → canonical_bytes` is byte-stable.

    Guarantees: contains no newline, so a log is a valid JSON Lines file.
    """
    record: dict[str, PayloadValue] = {}
    for spec in EVENT_FIELDS:
        value = getattr(event, spec.name)
        if spec.name == "type":
            record[spec.name] = EventType(value).value
        elif spec.name == "vclock":
            record[spec.name] = normalise_vclock(value)
        elif spec.name == "causal_parents":
            record[spec.name] = list(value)
        else:
            record[spec.name] = value
    record["payload"] = dict(event.payload)
    return encode_value(record)


def decode_event(line: str) -> Event:
    """Return the Event a canonical JSON line represents.

    Guarantees: `decode_event(encode_event(e))` reconstructs an event with the same
    canonical bytes as `e`. Uses the stdlib parser on input only — parsing is not part of
    the contract, emission is — and rejects floats so that a hand-edited or foreign log
    cannot smuggle one past `E-EVENT-013`.

    Migrates on read (PRD §9.9, `events/migrations/`) before constructing the `Event`:
    `check_structural`'s `E-EVENT-008` compares an already-built `Event`'s `schema_version`
    against the build's `SCHEMA_VERSION` with no tolerance for an older-but-migratable
    version, so migration must happen here, at the raw-record boundary, not downstream in
    validation. This is why every committed golden fixture — written at an earlier
    `SCHEMA_VERSION` — still validates and still hashes without its stored bytes ever
    changing: `migrate()` returns a new mapping, this function never rewrites the file it
    read `line` from.

    Raises:
        FloatNotPermittedError: the line contains a float (`E-EVENT-013`).
        MalformedEventLineError: a field has the wrong shape (`E-EVENT-015`).
        SchemaVersionError: `schema_version` is older than this build migrates, newer than
            this build knows, or missing (`E-EVENT-060`).
        KeyError: a required top-level field is absent.
        ValueError: the line is not valid JSON, or `type` is not a known event type.
    """
    import json

    from agentdx.events import migrations

    raw: object = json.loads(line, parse_float=_reject_float)
    if not isinstance(raw, Mapping):
        raise MalformedEventLineError(_NOT_AN_OBJECT)
    # `migrate()`'s precise `Mapping[str, PayloadValue]` return type is correct but too
    # narrow for the loose, per-field `int(raw[...])`/`str(raw[...])` coercions below (the
    # same shape this function already used on `raw` pre-migration, when it was freshly
    # parsed JSON). Round-tripping through `object` and re-narrowing via `isinstance`
    # — rather than a `cast(...)` — keeps every field extraction below exactly as it was
    # before migrate-on-read existed, with no explicit `Any` (AGENTS.md §4, `mypy --strict`'s
    # `disallow_any_explicit`) anywhere in this function.
    migrated: object = migrations.migrate(raw, to_version=SCHEMA_VERSION)
    if not isinstance(migrated, Mapping):  # pragma: no cover — migrate() always returns one
        raise MalformedEventLineError(_NOT_AN_OBJECT)
    raw = migrated
    payload = raw["payload"]
    if not isinstance(payload, Mapping):
        raise MalformedEventLineError(_PAYLOAD_NOT_OBJECT)
    vclock = raw["vclock"]
    if not isinstance(vclock, Mapping):
        raise MalformedEventLineError(_VCLOCK_NOT_OBJECT)
    parents = raw["causal_parents"]
    if not isinstance(parents, Sequence) or isinstance(parents, str):
        raise MalformedEventLineError(_PARENTS_NOT_ARRAY)

    return Event(
        schema_version=int(raw["schema_version"]),
        run_id=str(raw["run_id"]),
        seq=int(raw["seq"]),
        sched_step=int(raw["sched_step"]),
        virtual_ts_ms=int(raw["virtual_ts_ms"]),
        wall_ts_ms=int(raw["wall_ts_ms"]),
        vclock={str(k): int(v) for k, v in vclock.items()},
        type=EventType(raw["type"]),
        causal_parents=[int(p) for p in parents],
        payload={str(k): v for k, v in payload.items()},
        agent_id=_opt_str(raw.get("agent_id")),
        clock_slot=_opt_str(raw.get("clock_slot")),
        span_id=_opt_str(raw.get("span_id")),
        fault_id=_opt_str(raw.get("fault_id")),
    )


def _reject_float(text: str) -> int:
    """Reject any JSON number that is not an integer (ruling R4)."""
    raise FloatNotPermittedError(text)


def _opt_str(value: object) -> str | None:
    """Return a nullable string field, preserving None."""
    return None if value is None else str(value)
