"""Cache key construction (PRD §11.4, §11.5).

**Why this file duplicates, rather than imports, `sdk/providers/openai_compatible.py`'s
`cache_key_for`.** That function is real, tested P04 code and computes a closely related
algorithm — but it lives in `sdk/`, and the CONTEXT.md §4 layer contract is one-directional:
`sdk/` may import `runtime/`, `runtime/` must never import `sdk/`. P04 was built before
`runtime/cache/` existed (P07, this module), so it necessarily inlined its own key algorithm
rather than importing a module that did not exist yet. **Declared as a deviation** (see
`docs/cache.md` §6) rather than silently left unmentioned or silently fixed.

**This module's key is *not* proven byte-identical to the SDK's for every input, and this is
corrected here rather than left as the false claim an earlier version of this docstring made.**
`tests/unit/cache/test_key.py::test_matches_the_sdk_implementation_byte_for_byte` proves
agreement for `model`/message-text/`bool`/`int`/`str`-valued params, `bytes`, and `set`s of
those — the overwhelming common case, confirmed empirically by
`test_set_valued_params_match_the_sdk_when_the_elements_are_not_floats`. It does **not** hold
wherever a `float` value appears in the material — bare (`temperature=0.7`, arguably the single
most common real parameter) or
nested inside a `set`/list/dict (e.g. a `stop` set containing floats): the SDK's `stable_text`
embeds a float as a bare, unquoted `repr()`; this module instead builds `PayloadValue`-typed
material and hashes it through `agentdx.events.canonical.encode_value`, which **refuses raw
floats outright** (`FloatNotPermittedError`, ruling R4 — cross-platform float *serialisation*
is a determinism leak the canonical projection exists to forbid elsewhere in the system).
Python's `repr()` of a float is itself reproducible (a specified, platform-independent
shortest round-trip algorithm — this is not the same class of leak R4 guards against), so this
module represents a float as the string `f"float:{value!r}"` — deterministic, but a different
string than the SDK's unquoted embedding, hence a different final hash. It is *not* the case
that sets diverge as a category (a `set` of strings or ints matches the SDK byte-for-byte,
`test_matches_the_sdk_implementation_byte_for_byte`'s hypothesis strategy would have already
caught that); the divergence is specifically and only about floats, wherever they appear.
**A response recorded through the real SDK for a call with a float value anywhere in its
significant params will not be found by this module's own diagnostic tooling recomputing the
key** — a real, load-bearing limitation, listed in this response's NOT DONE/RISKS rather than
hidden behind a false equivalence claim.

**What is in the key, and why (PRD §11.4, design constraint 1).**

============================  ================================================================
In the key                    Reason
============================  ================================================================
``model``                     PRD §11.5: a different model is a different experiment; every
                               key changes when the model string changes, on purpose.
``messages`` (normalised)     What was actually asked. Whitespace is stripped only at message
                               boundaries (PRD §11.4) so that two prompts differing by one real
                               character never collide, and two prompts differing only in
                               trailing-whitespace formatting quirks do not spuriously miss.
``params`` (significant only) Only ``SIGNIFICANT_PARAMS`` — see the table below.
``tools``                     The tool schemas offered to the model. A different tool surface
                               is a different question, even for an identical prompt.
``response_fmt``              A different requested output shape is a different call.
``key_version``                Lets the algorithm change without silently reinterpreting old
                               keys as new ones — a version bump makes every old entry a clean,
                               explained miss instead of a wrong hit.
============================  ================================================================

**What is deliberately excluded, and why (PRD §11.4).**

============================  ================================================================
Excluded                      Reason
============================  ================================================================
``user``                      An arbitrary caller-supplied label; two calls that ask the same
                               question for two different end users must still hit the same
                               cache entry.
``stream``                    Changes how the response is delivered, not what is asked. The
                               cache stores the full response either way (PRD §11.6).
Request timeouts               Wall-clock transport concerns, invisible to the model.
Any machine-local salt         PRD §11.4 is explicit: none may ever enter the key, because a
                               salted key would make a cache non-portable between machines and
                               break `.agentdx` bundles (PRD §11.9) outright.
============================  ================================================================

**`SIGNIFICANT_PARAMS`** — sampling/decoding parameters that change what the model does:
``temperature``, ``top_p``, ``max_tokens``, ``stop``, ``seed``, ``frequency_penalty``,
``presence_penalty``, ``tool_choice``. Nothing outside this set participates in the key, even
if a caller passes it — this module reads only the named keys out of whatever mapping it is
given.

**Redaction (PRD §11.9): "`redact_patterns` are applied to prompts before hashing and
storage; a redaction changes the key, which is correct."** `normalise_messages` and every
function built on it accept an optional ``redact`` callable applied to every string of
message *content* before it is hashed or stored — exactly the PRD sentence, scoped exactly as
written ("prompts," i.e. message content; not `model`/`tools`/`response_format`, which are
not prose and are not where a secret plausibly appears). **This is the capability, not the
wiring**: the real, live SDK call site (`sdk/providers/openai_compatible.py`, P04, out of this
prompt's `DELIVERABLES`) computes its own key inline and does not call this module at all, so
`redact_patterns` still has zero effect on what a live run's cache actually stores until that
file is touched — the same shape of gap `docs/cache.md` §8 already declares for
`key_material`. Declared here, not silently left implicit.

**A value with no reproducible representation raises rather than hashing an address.**
`_as_payload_value`/`_normalise_part` used to fall back to ``hash_text(repr(value))`` for
anything outside the closed set below — and a plain Python object's default ``__repr__``
embeds its **memory address**, which differs every process. That made the cache key
process-local for exactly the kind of value the type signature (`Mapping[str, object]`)
explicitly allows a caller to pass (a multimodal part, a tool-call artefact). This mirrors a
real, already-shipped defence in the same codebase — `sdk/generic.py`'s `stable_text` detects
this exact case and raises `E-INSTR-008` instead of hashing an address. This module could not
reuse that function (`runtime/` must not import `sdk/`) so it duplicates the detection here
and raises `KeyMaterialError` (`E-CACHE-011`) under the same condition, rather than silently
degrading I1. `set`/`frozenset` values are sorted by their own stable encoding before joining
the key (a set's iteration order is not a contract — AGENTS.md §4.1's `sorted_set` clause,
duplicated here in spirit since `runtime/cache/` is not on `check_determinism_hygiene.py`'s
`ALLOWLIST` and does not construct any `set` itself, only sorts one it is handed).

**No ambient time, uuid or randomness.** This file is not on
`scripts/check_determinism_hygiene.py`'s `ALLOWLIST` and does not need to be: every input here
comes from the caller's own arguments, and the only hashing primitive used
(`agentdx.events.canonical.encode_value` + `blake2b`) is already pure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from hashlib import blake2b
from typing import Final

from agentdx.events.canonical import DIGEST_SIZE, HASH_PREFIX, encode_value
from agentdx.events.schema import PayloadValue

_DOCS: Final = "docs/cache.md"

_ADDRESS: Final = re.compile(r" at 0x[0-9a-fA-F]+")
"""Matches the address CPython's default `__repr__` embeds. A value whose representation
contains one cannot be hashed reproducibly — see the module docstring's note on
`KeyMaterialError`, and `sdk/generic.py`'s `stable_text`, which this pattern mirrors."""

KEY_VERSION: Final = 2
"""PRD §11.4. Included in every key's material (`key_material_for`'s `"key_version"` field),
so a bump changes every key's hash outright — every existing entry becomes an ordinary,
explained miss (`test_key_version_is_recorded_in_the_material_and_participates_in_the_key`)
rather than a silent wrong hit. **Not**, today, a distinct diagnostic message of its own: a
`describe_miss` (`modes.py`) call after a bump reports the same "closest stored key" text an
unrelated miss would, since `StoredEntry`/`MissCandidate` (`store.py`) do not carry the
candidate's own `key_version` for `describe_miss` to compare against the current one and call
out specifically. Declared here rather than implied by an earlier version of this docstring
that claimed a `key_version`-specific message already existed."""

SIGNIFICANT_PARAMS: Final = (
    "frequency_penalty",
    "max_tokens",
    "presence_penalty",
    "seed",
    "stop",
    "temperature",
    "tool_choice",
    "top_p",
)
"""PRD §11.4, sorted. Mirrors `sdk.providers.openai_compatible.SIGNIFICANT_PARAMS` exactly —
see the module docstring's note on the two implementations' provenance."""

KEY_EXCLUDED_PARAMS_DOC: Final = (
    "user — an arbitrary caller label, not a property of the question asked",
    "stream — delivery mechanism, not content; the cache always stores the full response",
    "timeouts — a wall-clock transport concern, invisible to the model",
)
"""Human-readable reasons for everything PRD §11.4 leaves out of the key. Exposed as data
(not just prose) so `docs/cache.md` and a future CLI ``--explain`` surface can print it
without re-deriving or duplicating it."""


class KeyMaterialError(RuntimeError):
    """A value offered to the cache key has no reproducible representation.

    Carries a stable `E-CACHE-0NN` code, in the same family `store.py`/`modes.py`/`perturb.py`
    use. Raised instead of silently hashing a value whose `repr()` embeds a memory address —
    see the module docstring.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the error from a stable code and a description of what went wrong."""
        self.code = code
        super().__init__(f"[{code}] {detail} ({_DOCS}#{code.lower()})")


def hash_text(text: str) -> str:
    """Return the ``blake2b:`` hash of a string, in the project's standard hash format.

    Guarantees: identical to the digest `sdk.providers.openai_compatible` computes for the
    same text via `sdk.generic.hash_text` — both use blake2b at `DIGEST_SIZE` with no salt,
    matching PRD §11.4's "the key never contains a machine-local salt."
    """
    return HASH_PREFIX + blake2b(text.encode("utf-8"), digest_size=DIGEST_SIZE).hexdigest()


def _reproducible_repr(value: object) -> str:
    """Return `repr(value)`, or raise `KeyMaterialError` if it embeds a memory address.

    Guarantees: never returns a string whose reproducibility depends on this process's
    memory layout. This is the one place non-`PayloadValue` values reach the key; every
    caller below routes through here rather than calling `repr()` directly.

    Raises:
        KeyMaterialError: `E-CACHE-011` the value's `repr()` embeds a memory address.
    """
    text = repr(value)
    if _ADDRESS.search(text):
        detail = (
            f"a value of type {type(value).__name__} has no reproducible representation "
            f"(its repr embeds a memory address), so the cache key would differ between "
            f"processes for the identical logical call — this is what PRD §11.4's 'the key "
            f"never contains a machine-local salt' guarantee forbids. Give the type a stable "
            f"__repr__, or convert it to a plain value (str/int/bool/list/dict) before "
            f"passing it as message content or a significant param"
        )
        raise KeyMaterialError("E-CACHE-011", detail)
    return text


def _normalise_part(part: object, redact: Callable[[str], str] | None) -> PayloadValue:
    """Return one multimodal message-content part, digesting anything that is not text.

    Guarantees: text parts are preserved (stripped, then redacted if `redact` is given) so a
    one-character prompt edit changes the key; every other part type (image, audio, ...) is
    represented by a content digest, which keeps the key small and keeps the cache portable —
    PRD §11.4's "represent images/audio by content digest."

    Raises:
        KeyMaterialError: `E-CACHE-011` — see `_reproducible_repr`.
    """
    if isinstance(part, Mapping):
        text = part.get("text")
        if part.get("type") == "text" and isinstance(text, str):
            stripped = text.strip()
            return {"type": "text", "text": redact(stripped) if redact else stripped}
        digestible = _as_payload_value(dict(part))
        return {
            "type": str(part.get("type", "unknown")),
            "digest": hash_text(encode_value(digestible)),
        }
    if isinstance(part, str):
        stripped = part.strip()
        return redact(stripped) if redact else stripped
    return {"type": "unknown", "digest": hash_text(_reproducible_repr(part))}


def _as_payload_value(value: object) -> PayloadValue:
    """Return ``value`` narrowed to `PayloadValue`.

    Guarantees: `bool`/`int`/`str`/`None` pass through unchanged; `float` becomes the string
    ``f"float:{value!r}"`` (Python's float `repr` is itself reproducible — see the module
    docstring; only its *embedding format* differs from the SDK's); `bytes`/`bytearray`
    become ``"bytes:<hex>"``; `Mapping`/`Sequence` recurse; `set`/`frozenset` are sorted by
    their own stable encoding first, since iteration order is not a contract. Anything else
    is folded into a content digest of its `repr()`.

    Raises:
        KeyMaterialError: `E-CACHE-011` the value's `repr()` has no reproducible
            representation (see `_reproducible_repr`) — this replaces a prior version of this
            function that silently hashed the address instead.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return f"float:{value!r}"
    if isinstance(value, bytes | bytearray):
        return "bytes:" + bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(k): _as_payload_value(v) for k, v in value.items()}
    if isinstance(value, frozenset | set):
        ordered = sorted((_as_payload_value(v) for v in value), key=encode_value)
        return ordered
    if isinstance(value, Sequence):
        return [_as_payload_value(v) for v in value]
    return {"type": "unknown", "digest": hash_text(_reproducible_repr(value))}


def normalise_messages(
    messages: Sequence[Mapping[str, object]],
    *,
    redact: Callable[[str], str] | None = None,
) -> list[dict[str, PayloadValue]]:
    """Return messages with whitespace stripped **at message boundaries only** (PRD §11.4).

    Guarantees: content is otherwise preserved exactly and no keys inside it are reordered
    relative to each other beyond the stable sort every canonical encoding applies, so two
    prompts differing by one character produce different keys.

    Args:
        messages: The messages to normalise.
        redact: When given, applied to every string of message *content* after boundary
            whitespace is stripped and before it is hashed or stored — PRD §11.9's
            "`redact_patterns` are applied to prompts before hashing and storage." Scoped to
            content only; see the module docstring on why `model`/`tools`/`response_format`
            are out of scope for this specific PRD sentence.

    Raises:
        KeyMaterialError: `E-CACHE-011` — see `_reproducible_repr`.
    """
    out: list[dict[str, PayloadValue]] = []
    for message in messages:
        item: dict[str, PayloadValue] = {}
        for key in sorted(message):
            value = message[key]
            if key == "content" and isinstance(value, str):
                stripped = value.strip()
                item[key] = redact(stripped) if redact else stripped
            elif key == "content" and isinstance(value, Sequence) and not isinstance(value, str):
                item[key] = [_normalise_part(part, redact) for part in value]
            else:
                item[key] = _as_payload_value(value)
        out.append(item)
    return out


def params_hash_for(params: Mapping[str, object]) -> str:
    """Return the hash of just the significant sampling parameters.

    Distinct from the cache key on purpose (mirrors `sdk.providers.openai_compatible`'s
    `params_hash_for`): this answers "was this call configured the same way?" independently
    of what was asked, which is what a redundancy pass (PRD §16.3) needs to group calls that
    differ only in prompt.

    Raises:
        KeyMaterialError: `E-CACHE-011` — see `_reproducible_repr`.
    """
    material: dict[str, PayloadValue] = {
        k: _as_payload_value(params[k]) for k in SIGNIFICANT_PARAMS if k in params
    }
    return hash_text(encode_value(material))


def key_material_for(
    model: str,
    messages: Sequence[Mapping[str, object]],
    params: Mapping[str, object],
    *,
    tools: Sequence[Mapping[str, object]] | None = None,
    response_format: object = None,
    redact: Callable[[str], str] | None = None,
) -> dict[str, PayloadValue]:
    """Return the PRD §11.4 key material, before hashing — the exact object the key covers.

    Exposed separately from `cache_key_for` so the cache store can persist it (see
    `store.py`) and use it for the "closest stored key" miss diagnostic (design constraint
    2) — a diagnostic that is impossible from the opaque hash alone.

    Args:
        model: The provider model identifier, included verbatim in the key material.
        messages: The conversation turns, normalised via `normalise_messages`.
        params: Call parameters; only `SIGNIFICANT_PARAMS` entries present in this
            mapping contribute to the key.
        tools: Tool/function definitions offered to the model, if any.
        response_format: The requested response format, if any.
        redact: See `normalise_messages`. PRD §11.9: a redaction changes the key, which is
            correct — a redacted prompt is a different prompt.

    Raises:
        KeyMaterialError: `E-CACHE-011` — see `_reproducible_repr`.
    """
    return {
        "model": model,
        "messages": normalise_messages(messages, redact=redact),
        "params": {k: _as_payload_value(params[k]) for k in SIGNIFICANT_PARAMS if k in params},
        "tools": [
            dict(sorted((k, _as_payload_value(v)) for k, v in tool.items()))
            for tool in (tools or ())
        ],
        "response_fmt": _as_payload_value(response_format),
        "key_version": KEY_VERSION,
    }


def key_material_json(
    model: str,
    messages: Sequence[Mapping[str, object]],
    params: Mapping[str, object],
    *,
    tools: Sequence[Mapping[str, object]] | None = None,
    response_format: object = None,
    redact: Callable[[str], str] | None = None,
) -> str:
    """Return the canonical JSON text of the key material — what `cache_key_for` hashes.

    Guarantees: `hash_text(key_material_json(...)) == cache_key_for(...)` always. Stored
    alongside every cache entry (see `store.py`) so a replay-mode miss can be explained
    without a debugger — design constraint 1.

    Raises:
        KeyMaterialError: `E-CACHE-011` — see `_reproducible_repr`.
    """
    return encode_value(
        key_material_for(
            model, messages, params, tools=tools, response_format=response_format, redact=redact
        )
    )


def cache_key_for(
    model: str,
    messages: Sequence[Mapping[str, object]],
    params: Mapping[str, object],
    *,
    tools: Sequence[Mapping[str, object]] | None = None,
    response_format: object = None,
    redact: Callable[[str], str] | None = None,
) -> str:
    """Return the PRD §11.4 cache key for one model call.

    Guarantees: **the key never contains a machine-local salt**, so a cache recorded on one
    machine replays on another — what makes `.agentdx` bundles work at all (PRD §11.9). Two
    calls with the identical logical content produce the identical key **in this process and
    across processes** — every value either narrows to a `PayloadValue` deterministically or
    raises `KeyMaterialError` rather than silently keying on a memory address.

    Only *approximately* equivalent to `sdk.providers.openai_compatible.cache_key_for` — see
    the module docstring for the precise, tested scope of that equivalence (proven for
    `bool`/`int`/`str`-valued params and text messages; known to diverge for `float`/`set`
    values).

    Raises:
        KeyMaterialError: `E-CACHE-011` — see `_reproducible_repr`.
    """
    return hash_text(
        key_material_json(
            model, messages, params, tools=tools, response_format=response_format, redact=redact
        )
    )


__all__ = [
    "KEY_EXCLUDED_PARAMS_DOC",
    "KEY_VERSION",
    "SIGNIFICANT_PARAMS",
    "KeyMaterialError",
    "cache_key_for",
    "hash_text",
    "key_material_for",
    "key_material_json",
    "normalise_messages",
    "params_hash_for",
]
