"""Design constraint 5: one interception point, three thin adapters (PRD §8.5).

Every test here uses an injected `httpx.MockTransport`, so the suite cannot reach the network
even by accident — which is also the property gate G9 asserts for the demo. The first test is
the one that keeps the design honest: it checks that all three providers take the *same* code
path and differ only in data.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

import agentdx
from agentdx.events.schema import EventType
from agentdx.events.validators import validate_log
from agentdx.sdk.generic import CachedResponse, use_run
from agentdx.sdk.providers import anthropic, groq
from agentdx.sdk.providers.openai_compatible import (
    KEY_VERSION,
    OpenAICompatibleClient,
    ProviderProfile,
    cache_key_for,
    normalise_messages,
)
from tests.unit.sdk.fakes import make_context

MESSAGES = [{"role": "user", "content": "summarise the plan"}]


def _response(text: str = "ok", model: str = "llama-3.1-8b-instant") -> httpx.Response:
    body = {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return httpx.Response(200, json=body)


class _MemoryCache:
    """A minimal stand-in for P07's cache."""

    def __init__(self) -> None:
        self.entries: dict[str, CachedResponse] = {}
        self.lookups: list[str] = []

    def lookup(self, cache_key: str) -> CachedResponse | None:
        self.lookups.append(cache_key)
        return self.entries.get(cache_key)

    def store(self, cache_key: str, response: CachedResponse) -> None:
        self.entries[cache_key] = response


# ---------------------------------------------------------------------------------------
# One path, three profiles
# ---------------------------------------------------------------------------------------


def test_all_three_providers_share_one_client_class() -> None:
    from agentdx.sdk.providers.openai_compatible import ProviderProfile as Profile

    openai_like = Profile(
        name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
    )
    clients = [
        groq.client(api_key="k"),
        anthropic.client(api_key="k"),
        OpenAICompatibleClient(openai_like, api_key="k"),
    ]
    assert {type(client) for client in clients} == {OpenAICompatibleClient}
    assert [client.profile.url() for client in clients] == [
        "https://api.groq.com/openai/v1/chat/completions",
        "https://api.anthropic.com/v1/chat/completions",
        "https://api.openai.com/v1/chat/completions",
    ]


def test_no_vendor_sdk_is_imported() -> None:
    # PRD §8.5 rejects a vendor SDK explicitly; ADR-004 fixes the permitted dependency set.
    import sys

    for forbidden in ("openai", "groq", "anthropic_sdk", "langchain_openai"):
        assert forbidden not in sys.modules


# ---------------------------------------------------------------------------------------
# Cache key construction (PRD §11.4)
# ---------------------------------------------------------------------------------------


def test_the_cache_key_ignores_insignificant_params() -> None:
    base = cache_key_for("m", MESSAGES, {"temperature": 0.0})
    assert base == cache_key_for("m", MESSAGES, {"temperature": 0.0, "user": "u1"})
    assert base == cache_key_for("m", MESSAGES, {"temperature": 0.0, "stream": True})
    assert base != cache_key_for("m", MESSAGES, {"temperature": 0.7})


def test_the_cache_key_changes_with_the_model() -> None:
    # PRD §11.5: a different model is a different experiment.
    assert cache_key_for("a", MESSAGES, {}) != cache_key_for("b", MESSAGES, {})


def test_the_cache_key_strips_whitespace_only_at_message_boundaries() -> None:
    padded = [{"role": "user", "content": "  summarise the plan\n"}]
    assert cache_key_for("m", MESSAGES, {}) == cache_key_for("m", padded, {})
    inner = [{"role": "user", "content": "summarise  the plan"}]
    assert cache_key_for("m", MESSAGES, {}) != cache_key_for("m", inner, {})


def test_the_cache_key_has_no_machine_local_salt() -> None:
    # This is what makes a bundle portable (PRD §11.4). Two identical calls, two processes,
    # one key — asserted here by the weaker but checkable property that the key is a pure
    # function of its inputs and carries the key version.
    first = cache_key_for("m", MESSAGES, {"seed": 42})
    second = cache_key_for("m", list(MESSAGES), {"seed": 42})
    assert first == second
    assert first.startswith("blake2b:")
    assert KEY_VERSION == 2


def test_non_text_content_parts_are_digested() -> None:
    parts = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    normalised = normalise_messages(parts)
    content = normalised[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert str(content[0]["digest"]).startswith("blake2b:")


# ---------------------------------------------------------------------------------------
# The PRD §11.2 mode table
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_mode_serves_from_the_cache_and_never_calls_out() -> None:
    cache = _MemoryCache()
    context, recorder = make_context(mode="replay", cache=cache)
    key = cache_key_for("llama-3.1-8b-instant", MESSAGES, {})
    cache.entries[key] = CachedResponse(
        body=json.dumps(
            {
                "model": "llama-3.1-8b-instant",
                "choices": [{"message": {"content": "cached answer"}}],
            }
        ),
        model="llama-3.1-8b-instant",
        prompt_tokens=3,
        completion_tokens=4,
    )

    def _explode(request: httpx.Request) -> httpx.Response:
        detail = "replay mode must never contact a provider"
        raise AssertionError(detail)

    client = groq.client(transport=httpx.MockTransport(_explode))

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context):
        assert await coder() == "cached answer"

    call = recorder.payloads(EventType.LLM_CALL)[0]
    assert call["cache_status"] == "hit"
    assert call["prompt_tokens"] == 3
    assert call["cache_key"] == key
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_replay_miss_is_a_hard_error_that_names_the_fix() -> None:
    # Invariant I7 / PRD §11.2: there is no fallback to a live call, and no flag that enables
    # one. The event is still written, so the log records the call that could not be served.
    context, recorder = make_context(mode="replay", cache=_MemoryCache())
    client = groq.client(transport=httpx.MockTransport(lambda r: _response()))

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context), pytest.raises(agentdx.CacheMissError) as caught:
        await coder()

    assert "E-CACHE-001" in str(caught.value)
    assert "agentdx run --record" in str(caught.value)
    call = recorder.payloads(EventType.LLM_CALL)[0]
    assert call["cache_status"] == "miss_error"


@pytest.mark.asyncio
async def test_record_mode_calls_out_once_and_stores_the_body() -> None:
    cache = _MemoryCache()
    context, recorder = make_context(mode="record", cache=cache)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _response("fresh answer")

    client = groq.client(api_key="test-key", transport=httpx.MockTransport(handler))

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context):
        assert await coder() == "fresh answer"
        assert await coder() == "fresh answer"

    assert len(calls) == 1, "the second call must be served from the cache it just filled"
    assert calls[0].headers["Authorization"] == "Bearer test-key"
    statuses = [payload["cache_status"] for payload in recorder.payloads(EventType.LLM_CALL)]
    assert statuses == ["miss_recorded", "hit"]
    validate_log(recorder.events)


@pytest.mark.asyncio
async def test_a_live_call_without_a_key_names_the_offline_path() -> None:
    context, _ = make_context(mode="record", cache=_MemoryCache())
    profile = ProviderProfile(
        name="groq",
        base_url="https://example.invalid/v1",
        default_model="m",
        api_key_env="AGENTDX_TEST_KEY_THAT_IS_NOT_SET",
    )
    client = OpenAICompatibleClient(profile)

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context), pytest.raises(agentdx.ProviderError) as caught:
        await coder()
    assert "E-LLM-001" in str(caught.value)
    assert 'mode = "replay"' in str(caught.value)


@pytest.mark.asyncio
async def test_a_provider_error_is_recorded_before_it_propagates() -> None:
    context, recorder = make_context(mode="record", cache=_MemoryCache())
    client = groq.client(
        api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    )

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context), pytest.raises(agentdx.ProviderError):
        await coder()

    assert recorder.payloads(EventType.LLM_CALL)[0]["cache_status"] == "miss_error"
    assert recorder.of_type(EventType.SPAN_END)[0].payload["status"] == "error"


@pytest.mark.asyncio
async def test_the_shim_yields_to_the_scheduler_around_the_call() -> None:
    # PRD §8.5 item 4: "Yields control to the scheduler around the call so that concurrency
    # is scheduler-visible even in passthrough mode."
    reasons: list[str] = []

    class RecordingScheduler:
        async def yield_point(self, reason: str) -> None:
            reasons.append(reason)

    cache = _MemoryCache()
    context, _ = make_context(mode="record", cache=cache)
    context = dataclasses.replace(context, scheduler=RecordingScheduler())
    client = groq.client(api_key="k", transport=httpx.MockTransport(lambda r: _response()))

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context):
        await coder()

    assert reasons == ["llm_call", "llm_call_done"]


@pytest.mark.asyncio
async def test_perturb_mode_labels_a_hit_as_perturbed() -> None:
    cache = _MemoryCache()
    context, recorder = make_context(mode="perturb", cache=cache)
    key = cache_key_for("llama-3.1-8b-instant", MESSAGES, {})
    cache.entries[key] = CachedResponse(body="{}", model="m", prompt_tokens=1, completion_tokens=1)
    client = groq.client(transport=httpx.MockTransport(lambda r: _response()))

    @agentdx.agent("coder")
    async def coder() -> None:
        await client.chat(MESSAGES)

    with use_run(context):
        await coder()

    assert recorder.payloads(EventType.LLM_CALL)[0]["cache_status"] == "perturbed"
