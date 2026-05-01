"""Anthropic model factory with explicit httpx timeouts.

Pydantic-AI's default `Agent("anthropic:...")` shortcut creates an Anthropic
client with no httpx timeout — a TCP stall during a tool call can hang
indefinitely. This module wires `httpx.AsyncClient(timeout=...)` into the
provider so every agent gets a hard upper bound per request.

A single `connect=10s, read=180s, write=10s, pool=10s` budget covers
Sonnet 4.6 categorization (~5s typical, ~30s tail) with comfortable headroom
while still surfacing a real "stuck" call within 3 minutes.
"""

from __future__ import annotations

import os

import httpx
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

# Numbers tuned to the largest known operation (50-tx categorization batch
# with WebSearchTool fan-out). Read-timeout is the upper bound on a single
# Anthropic streaming response; connect/write/pool catch hung sockets.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)


def make_anthropic_model(model: str) -> AnthropicModel | str:
    """Return an `AnthropicModel` with a timeout-bounded httpx client.

    Falls back to the bare `"anthropic:..."` model string when the
    `ANTHROPIC_API_KEY` env var is missing — this preserves legacy
    behaviour at module-import time for tests and dev environments
    without secrets, where pydantic-ai will use its lazy provider
    construction (and fail at first `.run()` instead of at import).
    """
    bare_id = model.removeprefix("anthropic:")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return model

    client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    provider = AnthropicProvider(api_key=api_key, http_client=client)
    return AnthropicModel(bare_id, provider=provider)
