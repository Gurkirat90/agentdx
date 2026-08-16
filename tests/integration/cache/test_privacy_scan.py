"""Definition of done #5: a plaintext scan under default config finds no prompt bodies.

**What "the cache DB" means here, resolved rather than assumed.** PRD §11.9 states plainly
that the *LLM response cache* (`llm_cache`, this module's `store.py`) "necessarily holds
bodies... replay requires it" — its privacy control is `0600` permissions and bundle
exclusion by default (§31.2, §31.3), never absence of content. The privacy control that is
actually about *absence* of prompt/response bodies by default is NFR-6 (PRD line ~4308):
"Never write prompt/response bodies to the event log by default; opt-in only [`capture_bodies`]
... Automated scan of the DB for plaintext after a default run" — the **event log's** own
database (`store/sqlite.py`, P03), not the LLM cache. Reading the mission's "cache DB" as the
LLM response cache and demanding it hold *no* prompt bodies would contradict PRD §11.9's own
requirement that it hold them; reading it as "the persistent store a default run writes,
scanned for plaintext" matches NFR-6's acceptance criterion verbatim. This test therefore
does both scans, so the distinction is demonstrated rather than asserted: the event log
(`store/sqlite.py`) is scanned and must be clean under the default `capture_bodies=False`;
the LLM cache (`runtime/cache/store.py`, this prompt's own deliverable) is scanned too, and
is shown to correctly, intentionally, contain the body — protected by `0600` instead.
Declared here per this prompt's STOP CONDITIONS rather than silently picking one reading.
"""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest

import agentdx
from agentdx.events.writer import EventWriter
from agentdx.runtime.cache.modes import Cache
from agentdx.runtime.cache.store import SqliteCacheStore
from agentdx.sdk.generic import use_run
from agentdx.sdk.providers import groq
from agentdx.store.sqlite import RunRecord, Store
from tests.unit.sdk.fakes import StampingRecorder, make_context

SECRET_PROMPT = "the launch codes are ZQ-118-ALPHA-DO-NOT-LEAK"  # noqa: S105 — test fixture text
SECRET_RESPONSE = "confirmed: ZQ-118-ALPHA-DO-NOT-LEAK is correct"  # noqa: S105 — test fixture
MESSAGES = [{"role": "user", "content": SECRET_PROMPT}]


def _response(text: str) -> httpx.Response:
    body = {
        "model": "llama-3.1-8b-instant",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 6},
    }
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_event_log_has_no_plaintext_body_under_the_default_capture_bodies_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-6's actual acceptance criterion: scan the event log DB for plaintext.

    Neither the prompt nor the response text may appear anywhere in the file's bytes under
    the default `capture_bodies=False` — not even inside a hash's hex digits, which is why
    the assertion checks for the literal secret text, not merely a field name.
    """
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    events_path = tmp_path / "events.db"
    cache_path = tmp_path / "cache.db"

    event_store = Store.open(events_path)
    event_store.create_run(
        RunRecord(
            run_id="r_privacy",
            scenario_hash="blake2b:" + "0" * 64,
            graph_hash="blake2b:" + "0" * 64,
            mode="record",
            seed=0,
            status="running",
            created_at="2026-08-15T00:00:00+00:00",
            agentdx_version="test",
        )
    )
    cache_store = SqliteCacheStore.open(cache_path)
    cache = Cache(backing_store=cache_store, mode="record", provider="groq")
    writer = EventWriter(run_id="r_privacy", sink=event_store)
    recorder = StampingRecorder("r_privacy", writer=writer)
    context, recorder = make_context(
        mode="record",
        cache=cache,
        run_id="r_privacy",
        recorder=recorder,
        capture_bodies=False,  # the default (agentdx.toml ships this)
    )
    client = groq.client(
        api_key="test-key", transport=httpx.MockTransport(lambda r: _response(SECRET_RESPONSE))
    )

    @agentdx.agent("coder")
    async def coder() -> str:
        return (await client.chat(MESSAGES)).text

    with use_run(context):
        answer = await coder()
    assert answer == SECRET_RESPONSE

    writer.flush()
    event_store.checkpoint()
    event_store.close()
    cache_store.close()

    events_bytes = events_path.read_bytes()
    assert SECRET_PROMPT.encode() not in events_bytes
    assert SECRET_RESPONSE.encode() not in events_bytes

    # sanity: the payload really was captured (as a hash), so a clean scan isn't just an
    # empty log — this confirms the LLM_CALL event was actually written and inspected.
    llm_calls = [e for e in recorder.events if e.type.value == "llm_call"]
    assert len(llm_calls) == 1
    assert "prompt" not in llm_calls[0].payload
    assert "response" not in llm_calls[0].payload
    assert llm_calls[0].payload["prompt_hash"].startswith("blake2b:")  # type: ignore[union-attr]


def test_llm_cache_db_correctly_and_intentionally_holds_the_body(tmp_path: Path) -> None:
    """The LLM cache is the opposite case by design (PRD §11.9).

    It must hold the body for replay to be possible at all. Its privacy control is `0600`,
    not absence of content — verified here directly, rather than asserted, so the two
    stores' different privacy models are both demonstrated, not assumed from one test.
    """
    from agentdx.runtime.cache.key import KEY_VERSION, cache_key_for, key_material_json
    from agentdx.runtime.cache.store import CachedResponse, response_hash_of

    cache_path = tmp_path / "cache.db"
    store = SqliteCacheStore.open(cache_path)
    key = cache_key_for("m", MESSAGES, {})
    response_body = f'{{"text": "{SECRET_RESPONSE}"}}'
    store.put(
        key,
        key_version=KEY_VERSION,
        model="m",
        prompt_hash=response_hash_of(SECRET_PROMPT),
        key_material=key_material_json("m", MESSAGES, {}),
        response=CachedResponse(
            body=response_body, model="m", prompt_tokens=9, completion_tokens=6
        ),
        provider="groq",
        response_hash=response_hash_of(response_body),
    )
    store.close()

    cache_bytes = cache_path.read_bytes()
    # the response body is present, by design (§11.9 — replay is impossible otherwise):
    assert SECRET_RESPONSE.encode() in cache_bytes
    # the prompt is present too, via key_material (this module's own declared deviation 1
    # in store.py's module docstring — needed for the "closest stored key" diagnostic):
    assert SECRET_PROMPT.encode() in cache_bytes

    # ...but the file is private, which is the actual control (PRD §31.2):
    mode = stat.S_IMODE(cache_path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
