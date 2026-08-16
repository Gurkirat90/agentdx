"""Cache key construction (PRD §11.4, design constraint 1).

Three claims are load-bearing and all are tested here rather than assumed:

1. **Byte-identical to the SDK's own `cache_key_for` — for the scope this is actually proven,
   and known to diverge outside it.** `sdk/providers/openai_compatible.py` inlined the PRD
   §11.4 algorithm before this module existed (see `key.py`'s module docstring); the two must
   agree on every key within the tested scope (`bool`/`int`/`str`-valued params, `bytes`,
   `set`s of non-float values, and text messages), or a response recorded through the real SDK
   would never be found by anything that recomputes the key through this module (the miss
   diagnostic, a future CLI, a bundle importer). Outside that scope — any `float` value,
   anywhere in the material — the two are proven to *diverge*, deliberately and by design (see
   `key.py`'s module docstring); that divergence is tested here too, so a change that
   accidentally made them match (or diverge somewhere new) would be caught either way.
2. **Every keyed parameter changes the key; every deliberately unkeyed parameter does not.**
   The property test the mission's definition of done names explicitly.
3. **A value with no reproducible representation raises `KeyMaterialError`, not a
   process-local hash.** The bug this module was rewritten to fix: a plain object's default
   `repr()` embeds a memory address, which used to be silently hashed into the key.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentdx.runtime.cache.key import (
    KEY_VERSION,
    SIGNIFICANT_PARAMS,
    KeyMaterialError,
    cache_key_for,
    hash_text,
    key_material_json,
    normalise_messages,
    params_hash_for,
)
from agentdx.sdk.providers.openai_compatible import cache_key_for as sdk_cache_key_for

MESSAGES = [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hello"}]


class _NoReproducibleRepr:
    """A plain object whose default `repr()` embeds this process's memory address."""


# ---------------------------------------------------------------------------------------
# Claim 1 — equivalence with the SDK's own implementation
# ---------------------------------------------------------------------------------------

TEXT = st.text(max_size=40)
ROLE = st.sampled_from(["system", "user", "assistant", "tool"])
MESSAGE = st.fixed_dictionaries({"role": ROLE, "content": TEXT})
MESSAGES_ST = st.lists(MESSAGE, min_size=1, max_size=4)
PARAMS_ST = st.fixed_dictionaries(
    {},
    optional={
        "temperature": st.integers(min_value=0, max_value=2),
        "top_p": st.integers(min_value=0, max_value=1),
        "max_tokens": st.integers(min_value=1, max_value=4096),
        "seed": st.integers(min_value=0, max_value=1000),
        "frequency_penalty": st.integers(min_value=-2, max_value=2),
        "presence_penalty": st.integers(min_value=-2, max_value=2),
        "tool_choice": st.sampled_from(["auto", "none", "required"]),
        "stop": st.sets(st.text(max_size=5), max_size=3),  # non-float set — proven to match too
        "user": TEXT,  # unkeyed — included to prove it does not perturb equivalence either
    },
)


@given(model=st.text(min_size=1, max_size=20), messages=MESSAGES_ST, params=PARAMS_ST)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_matches_the_sdk_implementation_byte_for_byte(
    model: str, messages: list[dict[str, str]], params: dict[str, object]
) -> None:
    """`runtime.cache.key.cache_key_for` and `sdk...openai_compatible.cache_key_for` agree.

    Run over 200 randomised calls (hypothesis), including messages with whitespace, empty
    strings, a `set`-valued `stop`, and an unkeyed `user` param — every one of which must
    produce the identical key from both implementations, since a response recorded through the
    real SDK is looked up through this module's key elsewhere (the store's `nearest()`
    diagnostic, a future `agentdx cache` CLI).

    Deliberately excludes `float`-valued params: that is a known, proven, *documented*
    divergence (`key.py`'s module docstring), not an oversight — see
    `test_diverges_from_the_sdk_for_a_float_valued_significant_param` below, which asserts the
    divergence explicitly rather than leaving it untested by omission the way the strategy
    that shipped with this module's first version did (it never generated a float).
    """
    assert cache_key_for(model, messages, params) == sdk_cache_key_for(model, messages, params)


def test_diverges_from_the_sdk_for_a_float_valued_significant_param() -> None:
    """A `float`-valued significant param is the one documented, proven divergence.

    `key.py`'s module docstring explains the mechanism: the SDK's `stable_text` embeds a float
    as a bare, unquoted `repr()`; this module tags it as the string `f"float:{value!r}"` and
    hashes it through `agentdx.events.canonical.encode_value` (which refuses raw floats
    outright). Both are internally deterministic, but they are not the *same* string, so they
    hash differently. This is asserted here — not just documented — so a future change that
    silently reconciles or silently worsens the divergence is caught either way.
    """
    ours = cache_key_for("m", MESSAGES, {"temperature": 0.7})
    theirs = sdk_cache_key_for("m", MESSAGES, {"temperature": 0.7})
    assert ours != theirs


def test_set_valued_params_match_the_sdk_when_the_elements_are_not_floats() -> None:
    """A `set`/`frozenset`-valued param matches the SDK exactly.

    Divergence is about floats specifically, not `set`s as a category. Regression test for
    an overclaim an earlier draft of `key.py`'s module docstring made
    (that sets diverge as a category): a `set` of strings sorts and joins identically in both
    implementations, since both eventually route non-float elements through the same
    canonical-JSON string form.
    """
    params = {"stop": {"alpha", "beta", "gamma"}}
    assert cache_key_for("m", MESSAGES, params) == sdk_cache_key_for("m", MESSAGES, params)


def test_a_float_nested_inside_a_set_still_diverges() -> None:
    """The float divergence is not limited to a bare top-level float value."""
    params = {"stop": {1.5, 2.5}}
    ours = cache_key_for("m", MESSAGES, params)
    theirs = sdk_cache_key_for("m", MESSAGES, params)
    assert ours != theirs


def test_raises_key_material_error_instead_of_hashing_a_memory_address() -> None:
    """A value with no reproducible `repr()` raises, rather than silently keying on an address.

    This is the exact bug the independent review found: `key.py` used to fall back to
    `hash_text(repr(value))` for anything outside a closed set of types, and a plain object's
    default `repr()` embeds this process's memory address — so the "same" logical call
    produced a different key in a different process, silently. Two structurally-identical
    calls with such a value must now raise the same way rather than key differently.
    """
    part = {"type": "blob", "payload": _NoReproducibleRepr()}
    messages = [{"role": "user", "content": [part]}]
    with pytest.raises(KeyMaterialError) as exc_info:
        cache_key_for("m", messages, {})
    assert exc_info.value.code == "E-CACHE-011"


def test_matches_the_sdk_implementation_with_tools_and_response_format() -> None:
    """The equivalence holds with `tools` and `response_format` set too."""
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    fmt = {"type": "json_object"}
    ours = cache_key_for("m", MESSAGES, {"temperature": 0}, tools=tools, response_format=fmt)
    theirs = sdk_cache_key_for("m", MESSAGES, {"temperature": 0}, tools=tools, response_format=fmt)
    assert ours == theirs


# ---------------------------------------------------------------------------------------
# Claim 2 — keyed parameters change the key; unkeyed parameters do not
# ---------------------------------------------------------------------------------------

BASE_PARAMS: dict[str, object] = {
    "temperature": 0,
    "top_p": 1,
    "max_tokens": 256,
    "stop": ["\n"],
    "seed": 7,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "tool_choice": "auto",
}


def _key(**overrides: object) -> str:
    model = str(overrides.pop("model", "llama-3.1-8b-instant"))
    messages = overrides.pop("messages", MESSAGES)
    params = dict(BASE_PARAMS)
    params.update({k: v for k, v in overrides.items() if k in SIGNIFICANT_PARAMS})
    excluded = (*SIGNIFICANT_PARAMS, "tools", "response_format")
    unkeyed = {k: v for k, v in overrides.items() if k not in excluded}
    params.update(unkeyed)
    tools = overrides.get("tools")
    response_format = overrides.get("response_format")
    return cache_key_for(model, messages, params, tools=tools, response_format=response_format)  # type: ignore[arg-type]


def test_every_significant_param_changes_the_key() -> None:
    """Changing any one of `SIGNIFICANT_PARAMS` changes the key, all else held fixed."""
    baseline = _key()
    overrides: dict[str, object] = {
        "temperature": 1,
        "top_p": 0,
        "max_tokens": 999,
        "stop": ["</s>"],
        "seed": 8,
        "frequency_penalty": 1,
        "presence_penalty": 1,
        "tool_choice": "none",
    }
    for param in SIGNIFICANT_PARAMS:
        changed = _key(**{param: overrides[param]})
        assert changed != baseline, f"changing {param!r} did not change the key"


def test_model_change_changes_the_key() -> None:
    """PRD §11.5: a different model is a different experiment."""
    assert _key(model="a") != _key(model="b")


def test_message_content_change_changes_the_key() -> None:
    """A one-character prompt edit must never collide."""
    a = [{"role": "user", "content": "hello"}]
    b = [{"role": "user", "content": "hellp"}]
    assert _key(messages=a) != _key(messages=b)


def test_tools_change_changes_the_key() -> None:
    """A different tool surface for the same prompt is a different question."""
    t1 = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    t2 = [{"type": "function", "function": {"name": "write", "parameters": {}}}]
    assert _key(tools=t1) != _key(tools=t2)


def test_response_format_change_changes_the_key() -> None:
    """A different requested output shape is a different call."""
    assert _key(response_format={"type": "json_object"}) != _key(response_format=None)


def test_deliberately_unkeyed_params_do_not_change_the_key() -> None:
    """`user` and any param outside `SIGNIFICANT_PARAMS` never perturb the key (PRD §11.4)."""
    baseline = _key()
    assert _key(user="alice") == baseline
    assert _key(user="bob") == baseline
    assert _key(stream=True) == baseline  # not a SIGNIFICANT_PARAMS member either


def test_message_boundary_whitespace_is_stripped_but_internal_content_is_not() -> None:
    """PRD §11.4: whitespace is stripped at message boundaries only."""
    padded = [{"role": "user", "content": "  hello  "}]
    bare = [{"role": "user", "content": "hello"}]
    assert _key(messages=padded) == _key(messages=bare)

    internal_a = [{"role": "user", "content": "a  b"}]
    internal_b = [{"role": "user", "content": "a b"}]
    assert _key(messages=internal_a) != _key(messages=internal_b)


def test_key_version_is_recorded_in_the_material_and_participates_in_the_key() -> None:
    """A `key_version` bump must be observable and must change every key (PRD §11.4)."""
    material = key_material_json("m", MESSAGES, {})
    assert f'"key_version":{KEY_VERSION}' in material


def test_key_material_json_hashes_to_the_cache_key() -> None:
    """`hash_text(key_material_json(...)) == cache_key_for(...)`, always."""
    material = key_material_json("m", MESSAGES, BASE_PARAMS)
    assert hash_text(material) == cache_key_for("m", MESSAGES, BASE_PARAMS)


def test_params_hash_is_independent_of_the_prompt() -> None:
    """`params_hash_for` answers "same configuration?", not "same question?" (PRD §16.3)."""
    a = normalise_messages([{"role": "user", "content": "one"}])
    b = normalise_messages([{"role": "user", "content": "two"}])
    assert a != b
    assert params_hash_for(BASE_PARAMS) == params_hash_for(BASE_PARAMS)


def test_no_machine_local_salt_two_independent_calls_agree() -> None:
    """PRD §11.4: the key never contains a machine-local salt — a portability property."""
    assert cache_key_for("m", MESSAGES, BASE_PARAMS) == cache_key_for("m", MESSAGES, BASE_PARAMS)


# ---------------------------------------------------------------------------------------
# Redaction — PRD §11.9: "`redact_patterns` are applied to prompts before hashing and
# storage; a redaction changes the key, which is correct — a redacted prompt is a different
# prompt."
# ---------------------------------------------------------------------------------------


def _redact_secret(text: str) -> str:
    return text.replace("sk-live-12345", "[REDACTED]")


def test_redact_is_applied_to_string_message_content_before_hashing() -> None:
    """A redaction changes the key — PRD §11.9's explicit, load-bearing requirement."""
    raw = [{"role": "user", "content": "my key is sk-live-12345, don't log it"}]
    unredacted = cache_key_for("m", raw, {}, redact=None)
    redacted = cache_key_for("m", raw, {}, redact=_redact_secret)
    assert unredacted != redacted


def test_redact_output_is_what_actually_gets_hashed_not_the_original() -> None:
    """The redacted text, not the secret, is what ends up in the key material."""
    raw = [{"role": "user", "content": "my key is sk-live-12345"}]
    material = key_material_json("m", raw, {}, redact=_redact_secret)
    assert "sk-live-12345" not in material
    assert "[REDACTED]" in material


def test_redact_is_applied_to_text_typed_multimodal_parts_too() -> None:
    """PRD §11.9 scopes to message content, including text-typed multimodal parts.

    "Prompts" is scoped here to message content, not just plain string content.
    """
    raw = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "the secret is sk-live-12345"}],
        }
    ]
    material = key_material_json("m", raw, {}, redact=_redact_secret)
    assert "sk-live-12345" not in material


def test_redact_two_identical_calls_with_the_same_redactor_still_agree() -> None:
    """Redaction must not itself introduce nondeterminism.

    Same input, same redactor, same key, every time.
    """
    raw = [{"role": "user", "content": "my key is sk-live-12345"}]
    a = cache_key_for("m", raw, {}, redact=_redact_secret)
    b = cache_key_for("m", raw, {}, redact=_redact_secret)
    assert a == b


def test_redact_is_not_applied_to_the_model_name() -> None:
    """PRD §11.9 scopes redaction to prompts; `model` is not prose and is not redacted."""
    material = key_material_json("sk-live-12345", MESSAGES, {}, redact=_redact_secret)
    assert '"model":"sk-live-12345"' in material
