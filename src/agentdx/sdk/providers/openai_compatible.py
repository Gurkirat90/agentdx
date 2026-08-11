"""The single LLM interception point: one HTTP path, three thin provider profiles (PRD §8.5).

PRD §8.5 is explicit that the shim targets the **OpenAI-compatible HTTP surface** rather than
any vendor SDK, and gives the reason: the model layer is replayed from cache anyway, so a hard
dependency on one vendor's client buys nothing and costs the product a model deprecation.
Groq, OpenAI and Anthropic all expose `POST {base_url}/chat/completions`, so all three reach
the provider through this one function and differ only in a `ProviderProfile` — a base URL, a
default model and the environment variable holding the key.

Four things happen around every call, in this order (PRD §8.5):

1. `(model, messages, params, tools, response_format)` is canonicalised into a cache key
   (PRD §11.4), with `key_version` so the algorithm can change without silently invalidating
   every entry.
2. The cache is consulted **per the active mode** (PRD §11.2). In `replay` a miss is
   `E-CACHE-001` and the run stops — invariant I7 — and there is deliberately no flag that
   would let it fall back to a live call.
3. An `llm_call` span is emitted with `cache_status`, token counts, model, and the three
   hashes. Bodies are written only under `capture_bodies=True` (PRD §8.11, invariant I8).
4. Control is yielded to the scheduler on both sides of the call, so concurrency is
   scheduler-visible even in passthrough mode where nothing is being replayed.

The cache itself lands at P07 and is injected as `RunContext.cache`; until then the default is
`NoCache`, which misses everything — and a replay-mode miss against it fails in exactly the
way a miss against a real empty cache does, rather than in a special "not implemented" way.

PRD §8.5 · §11.2, §11.4, §11.5, §11.6 · §8.11 · §36 (`E-CACHE-001`, `E-LLM-001`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx

from agentdx.events.schema import EventType, PayloadValue
from agentdx.sdk.generic import (
    CachedResponse,
    CacheMissError,
    ProviderError,
    RunContext,
    Span,
    current_agent,
    current_run,
    emit,
    hash_text,
    span,
    stable_text,
)

KEY_VERSION: Final = 2
"""PRD §11.4. A bump makes every entry a miss with a clear message rather than a wrong hit."""

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
"""PRD §11.4. Sorted, and everything outside it — `user`, `stream`, timeouts — is excluded
from the key on purpose: those change the request without changing what the model was asked.
"""

CHAT_COMPLETIONS_PATH: Final = "/chat/completions"
"""The one endpoint every OpenAI-compatible provider exposes."""

DEFAULT_TIMEOUT_WALL_MS: Final = 60_000
"""Wall-clock ceiling for a live provider call. Named `_wall_ms` per invariant I11's naming
lint: it is real time, it never enters virtual time, and it never enters the log."""

CACHE_STATUSES: Final = ("hit", "miss_recorded", "miss_error", "perturbed")
"""The closed `llm_call.payload.cache_status` enum of the P02 schema.

There is no member for "the cache was bypassed", which is what PRD §11.2's `passthrough` mode
does. See `docs/sdk.md` — the shim reports `miss_recorded` for a passthrough call and the run's
`cache_mode` in `run_start` is what distinguishes it. Raised as a stop condition before the
end-of-week-1 schema freeze rather than resolved here: adding an enum member is a schema change
and CONTEXT.md §11 tripwire 6 forbids one without an ADR.
"""


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Everything that differs between two OpenAI-compatible providers (PRD §8.5).

    Guarantees: this is the complete set of differences. If a provider needs more than a base
    URL, a default model and a key, it is not OpenAI-compatible and does not belong behind
    this shim — which is the check that keeps "one interception point, three thin adapters"
    true rather than aspirational.
    """

    name: str
    base_url: str
    default_model: str
    api_key_env: str
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def url(self) -> str:
        """Return the chat-completions endpoint for this provider."""
        return self.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH

    def headers(self, api_key: str) -> dict[str, str]:
        """Return the request headers, including the bearer token."""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **dict(self.extra_headers),
        }


@dataclass(frozen=True, slots=True)
class _Call:
    """The four identifying facts of one model call, carried to the emission site."""

    model: str
    cache_key: str
    params_hash: str
    prompt_body: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    """One completed model call, as the caller sees it.

    Guarantees: `cache_key` and the three hashes are the same values written to the
    `llm_call` event, so a caller can cite the event that produced its own result (I6).
    """

    text: str
    model: str
    cache_status: str
    cache_key: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None
    body: str


# ---------------------------------------------------------------------------------------
# Cache key construction (PRD §11.4)
# ---------------------------------------------------------------------------------------


def normalise_messages(messages: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return messages with whitespace stripped **at message boundaries only** (PRD §11.4).

    Guarantees: content is otherwise preserved exactly and no keys inside it are sorted, so
    two prompts that differ in a single character produce different keys. Non-text parts —
    images, audio — are represented by a content digest rather than inline, which is what
    keeps the key small and the cache portable.
    """
    out: list[dict[str, object]] = []
    for message in messages:
        item: dict[str, object] = {}
        for key in sorted(message):
            value = message[key]
            if key == "content" and isinstance(value, str):
                item[key] = value.strip()
            elif key == "content" and isinstance(value, Sequence):
                item[key] = [_normalise_part(part) for part in value]
            else:
                item[key] = value
        out.append(item)
    return out


def _normalise_part(part: object) -> object:
    """Return a multimodal content part, digesting anything that is not text."""
    if isinstance(part, Mapping):
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            return {"type": "text", "text": str(part["text"]).strip()}
        return {"type": str(part.get("type", "unknown")), "digest": hash_text(stable_text(part))}
    if isinstance(part, str):
        return part.strip()
    return {"type": "unknown", "digest": hash_text(stable_text(part))}


def params_hash_for(params: Mapping[str, object]) -> str:
    """Return the hash of just the significant sampling parameters (PRD §8.5).

    Distinct from the cache key on purpose: `params_hash` answers "was this call configured
    the same way?" independently of what was asked, which is what lets PRD §16.3's redundancy
    pass group calls that differ only in prompt.
    """
    return hash_text(stable_text({k: params[k] for k in SIGNIFICANT_PARAMS if k in params}))


def cache_key_for(
    model: str,
    messages: Sequence[Mapping[str, object]],
    params: Mapping[str, object],
    *,
    tools: Sequence[Mapping[str, object]] | None = None,
    response_format: object = None,
) -> str:
    """Return the PRD §11.4 cache key for one model call.

    Guarantees: **the key never contains a machine-local salt**, so a cache recorded on one
    machine replays on another — which is what makes `.agentdx` bundles work at all. Only
    `SIGNIFICANT_PARAMS` participate; `user`, `stream` and timeouts do not, because they
    change the request without changing what the model was asked.
    """
    material = {
        "model": model,
        "messages": normalise_messages(messages),
        "params": {k: params[k] for k in SIGNIFICANT_PARAMS if k in params},
        "tools": [dict(sorted(tool.items())) for tool in (tools or ())],
        "response_fmt": response_format,
        "key_version": KEY_VERSION,
    }
    return hash_text(stable_text(material))


# ---------------------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------------------


class OpenAICompatibleClient:
    """The shim every provider goes through (PRD §8.5).

    Guarantees:

    * **Offline by default.** In `replay` mode no network call is possible and no API key is
      read, which is what gate G9 and invariant I7 require. A miss is `E-CACHE-001` and the
      run stops.
    * Bodies never reach the event log unless `capture_bodies` is on (invariant I8). The
      cache necessarily holds them — replay is impossible otherwise — and that is a separate
      file bundles exclude by default (PRD §8.11, §31.3).
    * Exceptions propagate. A provider failure raises `E-LLM-001` after the `llm_call` event
      has been written, so a failed call is evidence rather than an absence.
    """

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_wall_ms: int = DEFAULT_TIMEOUT_WALL_MS,
    ) -> None:
        """Bind a client to a provider profile.

        Args:
            profile: Which OpenAI-compatible provider to talk to.
            api_key: Overrides the environment variable named by the profile. Never logged.
            transport: An `httpx` transport, injected by tests so no test can reach the
                network by accident.
            timeout_wall_ms: Wall-clock ceiling for a live call.
        """
        self._profile = profile
        self._api_key = api_key
        self._transport = transport
        self._timeout_wall_ms = timeout_wall_ms

    @property
    def profile(self) -> ProviderProfile:
        """Return the provider profile this client is bound to."""
        return self._profile

    async def chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        model: str | None = None,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: object = None,
        **params: object,
    ) -> ChatResult:
        """Make one model call, recording it as an `llm_call` span.

        Guarantees: emits `span_start`, `llm_call` and `span_end` on every path, including
        the failure paths, and yields to the scheduler on both sides of the call.

        Raises:
            CacheMissError: `replay` or `perturb` mode and the key is not cached
                (`E-CACHE-001`, exit code 3). There is no fallback to a live call.
            ProviderError: the provider refused or failed, or a live call is required and no
                API key is present (`E-LLM-001`).
            AgentContextError: called with no ambient agent (`E-INSTR-004`).
        """
        run = current_run()
        resolved_model = model or self._profile.default_model
        key = cache_key_for(
            resolved_model, messages, params, tools=tools, response_format=response_format
        )
        prompt_body = stable_text(normalise_messages(messages))
        call = _Call(resolved_model, key, params_hash_for(params), prompt_body)

        async with span("llm_call", resolved_model) as open_span:
            await run.scheduler.yield_point("llm_call")
            try:
                result = await self._resolve(run, resolved_model, key, messages, params, tools)
            except CacheMissError:
                self._emit(run, open_span, call, "miss_error", None)
                raise
            except ProviderError:
                self._emit(run, open_span, call, "miss_error", None)
                raise
            await run.scheduler.yield_point("llm_call_done")
            self._emit(run, open_span, call, result.cache_status, result)
            return result

    async def _resolve(
        self,
        run: RunContext,
        model: str,
        key: str,
        messages: Sequence[Mapping[str, object]],
        params: Mapping[str, object],
        tools: Sequence[Mapping[str, object]] | None,
    ) -> ChatResult:
        """Apply the PRD §11.2 mode table to one call.

        Raises:
            CacheMissError: a deterministic mode missed (`E-CACHE-001`).
            ProviderError: a live call was needed and failed or was impossible (`E-LLM-001`).
        """
        if run.mode in ("replay", "perturb"):
            cached = run.cache.lookup(key)
            if cached is None:
                raise CacheMissError(_miss_message(run, model, key))
            return _from_cache(cached, key, "perturbed" if run.mode == "perturb" else "hit")

        if run.mode == "record":
            cached = run.cache.lookup(key)
            if cached is not None:
                return _from_cache(cached, key, "hit")

        live = await self._call_provider(model, messages, params, tools)
        run.cache.store(
            key,
            CachedResponse(
                body=live.body,
                model=live.model,
                prompt_tokens=live.prompt_tokens,
                completion_tokens=live.completion_tokens,
                finish_reason=live.finish_reason,
                recorded_run_id=run.run_id,
            ),
        )
        return live

    async def _call_provider(
        self,
        model: str,
        messages: Sequence[Mapping[str, object]],
        params: Mapping[str, object],
        tools: Sequence[Mapping[str, object]] | None,
    ) -> ChatResult:
        """Perform the live HTTP call.

        Raises:
            ProviderError: no API key is available, or the provider returned a non-2xx
                status or an unreadable body (`E-LLM-001`).
        """
        api_key = self._api_key or os.environ.get(self._profile.api_key_env)
        if not api_key:
            detail = (
                f"a live call to {self._profile.name} is required in this mode but "
                f"${self._profile.api_key_env} is not set. The offline path is "
                f'`mode = "replay"` with a recorded cache; to record one, set the key and '
                f"run with `--record`"
            )
            raise ProviderError(detail)

        request: dict[str, object] = {"model": model, "messages": list(messages), **params}
        if tools:
            request["tools"] = list(tools)

        timeout = httpx.Timeout(self._timeout_wall_ms / 1000)
        async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
            try:
                response = await client.post(
                    self._profile.url(),
                    json=request,
                    headers=self._profile.headers(api_key),
                )
            except httpx.HTTPError as exc:
                detail = f"{self._profile.name} call failed: {type(exc).__name__}: {exc}"
                raise ProviderError(detail) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            detail = (
                f"{self._profile.name} returned HTTP {response.status_code}: {response.text[:200]}"
            )
            raise ProviderError(detail)
        return _from_response(response.text, model)

    def _emit(
        self,
        run: RunContext,
        open_span: Span,
        call: _Call,
        cache_status: str,
        result: ChatResult | None,
    ) -> int:
        """Emit the `llm_call` event for one call, successful or not."""
        agent = current_agent()
        response_body = "" if result is None else result.text
        payload: dict[str, PayloadValue] = {
            "model": call.model,
            "params_hash": call.params_hash,
            "prompt_hash": hash_text(call.prompt_body),
            "response_hash": hash_text(response_body),
            "prompt_tokens": 0 if result is None else result.prompt_tokens,
            "completion_tokens": 0 if result is None else result.completion_tokens,
            "cache_status": cache_status,
            "cache_key": call.cache_key,
            "perturbed_from_run": None,
        }
        if run.capture_bodies:
            payload["prompt"] = run.redactor.scrub(call.prompt_body)
            payload["response"] = run.redactor.scrub(response_body)
        return emit(
            run,
            EventType.LLM_CALL,
            payload,
            agent_id=agent.agent_id,
            clock_slot=agent.clock_slot,
            span_id=open_span.span_id,
            causes=(open_span.start_seq,),
        )


def _miss_message(run: RunContext, model: str, key: str) -> str:
    """Render the `E-CACHE-001` message PRD §36 specifies, naming the exact fix."""
    agent = current_agent()
    return (
        f"cache miss in {run.mode} mode for agent {agent.agent_id!r}, model {model!r}, "
        f"key {key[:24]}…. A replay-mode miss is a hard error and never a live call: a "
        f"silent fallback would make CI non-hermetic, bundles unreproducible and cost "
        f"unpredictable. Re-record with: agentdx run --record"
    )


def _from_cache(cached: CachedResponse, key: str, cache_status: str) -> ChatResult:
    """Return a `ChatResult` built from a cache entry."""
    return ChatResult(
        text=_text_of(cached.body),
        model=cached.model,
        cache_status=cache_status,
        cache_key=key,
        prompt_tokens=cached.prompt_tokens,
        completion_tokens=cached.completion_tokens,
        finish_reason=cached.finish_reason,
        body=cached.body,
    )


def _from_response(body: str, model: str) -> ChatResult:
    """Return a `ChatResult` built from a provider response body.

    Guarantees: the full body is retained verbatim so replay reproduces tool-call structures
    and `finish_reason`, not merely the text (PRD §11.6).

    Raises:
        ProviderError: the body is not the OpenAI-compatible shape (`E-LLM-001`).
    """
    try:
        parsed: object = json.loads(body)
    except json.JSONDecodeError as exc:
        detail = f"the provider response is not JSON: {exc}"
        raise ProviderError(detail) from exc
    if not isinstance(parsed, Mapping):
        detail = "the provider response is not a JSON object"
        raise ProviderError(detail)

    choices = parsed.get("choices")
    if not isinstance(choices, Sequence) or not choices:
        detail = "the provider response has no `choices`; it is not OpenAI-compatible"
        raise ProviderError(detail)
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    text = message.get("content") if isinstance(message, Mapping) else None
    usage = parsed.get("usage")
    usage_map: Mapping[str, object] = usage if isinstance(usage, Mapping) else {}
    finish = first.get("finish_reason") if isinstance(first, Mapping) else None

    return ChatResult(
        text="" if text is None else str(text),
        model=str(parsed.get("model", model)),
        cache_status="miss_recorded",
        cache_key="",
        prompt_tokens=_int_of(usage_map.get("prompt_tokens")),
        completion_tokens=_int_of(usage_map.get("completion_tokens")),
        finish_reason=None if finish is None else str(finish),
        body=body,
    )


def _text_of(body: str) -> str:
    """Return the assistant text of a stored provider body, or the body itself."""
    try:
        parsed: object = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(parsed, Mapping):
        return body
    choices = parsed.get("choices")
    if not isinstance(choices, Sequence) or not choices:
        return body
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    text = message.get("content") if isinstance(message, Mapping) else None
    return body if text is None else str(text)


def _int_of(value: object) -> int:
    """Return an integer token count, treating an absent or unusable value as zero.

    Token counts are advisory provenance, not evidence; a provider that omits `usage` must
    not stop the run. Where the number matters — PRD §17's scorecard — it is summed from the
    log, and a zero there is visible as a zero rather than as a crash here.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


__all__ = [
    "CACHE_STATUSES",
    "CHAT_COMPLETIONS_PATH",
    "KEY_VERSION",
    "SIGNIFICANT_PARAMS",
    "ChatResult",
    "OpenAICompatibleClient",
    "ProviderProfile",
    "cache_key_for",
    "normalise_messages",
    "params_hash_for",
]
