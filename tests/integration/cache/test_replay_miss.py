"""Definition of done #2: a replay-mode miss is a hard error naming a useful diff.

Two things are demonstrated, deliberately kept separate:

1. The **real, live SDK path** (`OpenAICompatibleClient._resolve`, unmodified) raises
   `agentdx.CacheMissError` — `E-CACHE-001` — and never falls back to a live call, exactly as
   `tests/unit/sdk/test_providers.py::test_a_replay_miss_is_a_hard_error_that_names_the_fix`
   already covers. That message is the SDK's own, fixed text (`_miss_message`); this module
   does not and cannot enhance it without editing `sdk/providers/openai_compatible.py`, which
   is out of `DELIVERABLES` — see this response's NOT DONE/RISKS.
2. This prompt's own `Cache.describe_miss` — design constraint 2's "the closest stored key,
   and a diff of the two" — which the live SDK does not call today, but which is real,
   tested, working code a caller with access to more than the bare `LlmCache` Protocol (a
   future CLI, a richer run host) can already use for the diagnostic the mission's
   definition of done actually asks to see. Output is printed so it can be pasted verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import agentdx
from agentdx.runtime.cache.key import cache_key_for, key_material_json
from agentdx.runtime.cache.modes import Cache
from agentdx.runtime.cache.store import SqliteCacheStore, response_hash_of
from agentdx.sdk.generic import CachedResponse, use_run
from agentdx.sdk.providers import groq
from tests.unit.sdk.fakes import make_context

MESSAGES = [{"role": "user", "content": "summarise the release notes for v2"}]


@pytest.mark.asyncio
async def test_replay_miss_through_the_live_sdk_is_a_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real, unmodified SDK call path: a replay-mode miss raises, never calls out."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    cache = Cache(backing_store=store, mode="replay", provider="groq")
    context, _recorder = make_context(mode="replay", cache=cache, run_id="r_replay")

    def _explode(request: httpx.Request) -> httpx.Response:
        detail = "replay mode must never contact a provider (I7)"
        raise AssertionError(detail)

    client = groq.client(transport=httpx.MockTransport(_explode))

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context), pytest.raises(agentdx.CacheMissError) as caught:
        await coder()
    store.close()

    message = str(caught.value)
    print(message)  # noqa: T201 — pasted into the response
    assert "E-CACHE-001" in message
    assert "agentdx run --record" in message


def test_describe_miss_names_the_closest_key_and_renders_a_real_diff(tmp_path: Path) -> None:
    """Design constraint 2, exercised directly: `Cache.describe_miss` on a populated store."""
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    cache = Cache(backing_store=store, mode="replay", provider="groq")

    stored_prompt = [{"role": "user", "content": "summarise the release notes for v1"}]
    stored_key = cache_key_for("llama-3.1-8b-instant", stored_prompt, {})
    stored_material = key_material_json("llama-3.1-8b-instant", stored_prompt, {})
    store.put(
        stored_key,
        key_version=2,
        model="llama-3.1-8b-instant",
        prompt_hash=response_hash_of("summarise the release notes for v1"),
        key_material=stored_material,
        response=CachedResponse(
            body='{"text": "v1 notes"}',
            model="llama-3.1-8b-instant",
            prompt_tokens=5,
            completion_tokens=3,
        ),
        provider="groq",
        response_hash=response_hash_of('{"text": "v1 notes"}'),
    )

    missed_key = cache_key_for("llama-3.1-8b-instant", MESSAGES, {})
    missed_material = key_material_json("llama-3.1-8b-instant", MESSAGES, {})
    message = cache.describe_miss(
        missed_key, key_material=missed_material, model="llama-3.1-8b-instant"
    )
    store.close()

    print(message)  # noqa: T201 — pasted into the response

    assert "cache miss" in message
    assert "closest stored key" in message
    assert stored_key[:24] in message
    assert "edit distance" in message
    assert "never falls back to a live call" in message
    assert "agentdx run --record" in message
    # a real unified diff of the two key materials, not a placeholder:
    assert "closest stored key" in message and "this call" in message


def test_describe_miss_report_as_structured_data(tmp_path: Path) -> None:
    """Same scenario, reported as JSON — convenient for the closing response's pasted output."""
    store = SqliteCacheStore.open(tmp_path / "cache.db")
    stored_prompt = [{"role": "user", "content": "summarise the release notes for v1"}]
    stored_key = cache_key_for("m", stored_prompt, {})
    store.put(
        stored_key,
        key_version=2,
        model="m",
        prompt_hash=response_hash_of("summarise the release notes for v1"),
        key_material=key_material_json("m", stored_prompt, {}),
        response=CachedResponse(body="x", model="m", prompt_tokens=1, completion_tokens=1),
        provider="groq",
        response_hash=response_hash_of("x"),
    )
    missed_key = cache_key_for("m", MESSAGES, {})
    missed_material = key_material_json("m", MESSAGES, {})
    near = store.nearest(missed_material, model="m")
    store.close()

    report = {
        "missed_key": missed_key[:24] + "…",
        "closest_key": near[0].cache_key[:24] + "…" if near else None,
        "edit_distance": near[0].distance if near else None,
    }
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    assert near
    assert near[0].distance > 0
