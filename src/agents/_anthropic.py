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
from dataclasses import replace

import httpx
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.profiles.anthropic import anthropic_model_profile
from pydantic_ai.providers.anthropic import AnthropicProvider

# Numbers tuned to the largest known operation (50-tx categorization batch
# with WebSearchTool fan-out). Read-timeout is the upper bound on a single
# Anthropic streaming response; connect/write/pool catch hung sockets.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)


def make_anthropic_model(
    model: str,
    *,
    prefer_prompted_output: bool = False,
) -> AnthropicModel | str:
    """Return an `AnthropicModel` with a timeout-bounded httpx client.

    Falls back to the bare `"anthropic:..."` model string when the
    `ANTHROPIC_API_KEY` env var is missing — this preserves legacy
    behaviour at module-import time for tests and dev environments
    without secrets, where pydantic-ai will use its lazy provider
    construction (and fail at first `.run()` instead of at import).

    When ``prefer_prompted_output`` is true, AutoOutputSchema resolves to
    prompted JSON output instead of the default output-tool mode. This avoids
    brittle final-result tool calls for the narrative analysis agents without
    using Anthropic native structured output, whose strict schema validator
    rejects our intentionally free-form ``Observation.metrics`` object.
    TestModel overrides keep their own default test-friendly mode.
    """
    bare_id = model.removeprefix("anthropic:")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return model

    client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    provider = AnthropicProvider(api_key=api_key, http_client=client)
    profile = None
    if prefer_prompted_output:
        base_profile = anthropic_model_profile(bare_id)
        if base_profile:
            profile = replace(base_profile, default_structured_output_mode="prompted")
    return AnthropicModel(bare_id, provider=provider, profile=profile)
