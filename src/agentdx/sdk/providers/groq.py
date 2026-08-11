"""Groq, as a profile over the OpenAI-compatible shim (PRD §8.5, CONTEXT.md §3).

Groq is the **default recording configuration** — CONTEXT.md §3 locks Llama 3.1 8B on the
free tier as the model layer — but it is not a code path. Everything here is data: a base URL,
a default model and the environment variable holding the key. There is no `groq` distribution
in `pyproject.toml` and there will not be one; PRD §8.5 rejects a vendor SDK explicitly, on
the grounds that the model layer is replayed from cache anyway and a hard dependency on one
vendor's client buys a model deprecation risk for nothing.

If this file ever grows a request transformation, that is the signal that the provider is not
actually OpenAI-compatible and the shim's "one interception point, three thin adapters"
promise has been broken.
"""

from __future__ import annotations

from typing import Final

import httpx

from agentdx.sdk.providers.openai_compatible import OpenAICompatibleClient, ProviderProfile

GROQ: Final = ProviderProfile(
    name="groq",
    base_url="https://api.groq.com/openai/v1",
    default_model="llama-3.1-8b-instant",
    api_key_env="GROQ_API_KEY",
)
"""The PRD §8.7 `[llm]` defaults, as a profile. Configuration overrides all three fields."""


def client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenAICompatibleClient:
    """Return a shim client bound to Groq.

    Guarantees: reads no environment variable at construction. The key is looked up only
    when a mode that requires a live call actually needs one, so a `replay`-mode run works
    with no key in the environment at all — which is what gate G9 asserts.

    Args:
        api_key: Overrides `$GROQ_API_KEY`. Never logged and never hashed into a cache key.
        base_url: Overrides the default endpoint, for a proxy or a compatible gateway.
        model: Overrides the default model. Changing it invalidates every cache entry, which
            is correct: a different model is a different experiment (PRD §11.5).
        transport: An `httpx` transport, injected by tests so no test reaches the network.
    """
    profile = ProviderProfile(
        name=GROQ.name,
        base_url=base_url or GROQ.base_url,
        default_model=model or GROQ.default_model,
        api_key_env=GROQ.api_key_env,
    )
    return OpenAICompatibleClient(profile, api_key=api_key, transport=transport)


__all__ = ["GROQ", "client"]
