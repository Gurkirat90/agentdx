"""Anthropic, as a profile over the OpenAI-compatible shim (PRD §8.5).

Anthropic publishes an OpenAI-compatible surface at `https://api.anthropic.com/v1`, which
exposes `POST /chat/completions` and accepts a bearer token. That is the surface this profile
targets, for the same reason as Groq's: PRD §8.5 rejects a vendor SDK, and reaching all three
providers through one HTTP path is what makes the shim a single interception point rather than
three parallel implementations that drift.

**What this profile does not do.** It does not translate between the Anthropic Messages API
and the OpenAI schema. If a caller needs a Messages-API-only feature, the honest answer is
that the shim does not support it — not a translation layer that silently drops fields, which
would change what the model was asked without changing the cache key and make replay a lie.
"""

from __future__ import annotations

from typing import Final

import httpx

from agentdx.sdk.providers.openai_compatible import OpenAICompatibleClient, ProviderProfile

ANTHROPIC: Final = ProviderProfile(
    name="anthropic",
    base_url="https://api.anthropic.com/v1",
    default_model="claude-haiku-4-5",
    api_key_env="ANTHROPIC_API_KEY",
)
"""Anthropic's OpenAI-compatible endpoint. Not a v1 default — CONTEXT.md §3 locks Groq as the
default recording configuration; this profile exists so a user is not locked to one vendor."""


def client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenAICompatibleClient:
    """Return a shim client bound to Anthropic's OpenAI-compatible surface.

    Guarantees: identical semantics to every other provider — same cache key construction,
    same mode table, same `llm_call` event. A run recorded against one provider and replayed
    against another replays from the cache and never contacts either (PRD §11.5).

    Args:
        api_key: Overrides `$ANTHROPIC_API_KEY`. Never logged and never hashed into a key.
        base_url: Overrides the default endpoint.
        model: Overrides the default model. Changing it invalidates every cache entry, which
            is correct: a different model is a different experiment (PRD §11.5).
        transport: An `httpx` transport, injected by tests so no test reaches the network.
    """
    profile = ProviderProfile(
        name=ANTHROPIC.name,
        base_url=base_url or ANTHROPIC.base_url,
        default_model=model or ANTHROPIC.default_model,
        api_key_env=ANTHROPIC.api_key_env,
    )
    return OpenAICompatibleClient(profile, api_key=api_key, transport=transport)


__all__ = ["ANTHROPIC", "client"]
