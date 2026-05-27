"""Tests for Anthropic model factory behaviour."""

from __future__ import annotations

from pydantic_ai.models.anthropic import AnthropicModel

from src.agents._anthropic import make_anthropic_model


def test_prompted_output_opt_in_switches_auto_structured_mode(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy-key-not-used")

    model = make_anthropic_model(
        "anthropic:claude-sonnet-4-6",
        prefer_prompted_output=True,
    )

    assert isinstance(model, AnthropicModel)
    assert model.profile.supports_json_schema_output is True
    assert model.profile.default_structured_output_mode == "prompted"


def test_default_structured_mode_stays_tool_for_existing_agents(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy-key-not-used")

    model = make_anthropic_model("anthropic:claude-sonnet-4-6")

    assert isinstance(model, AnthropicModel)
    assert model.profile.default_structured_output_mode == "tool"


def test_missing_api_key_keeps_lazy_string_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    model = make_anthropic_model(
        "anthropic:claude-sonnet-4-6",
        prefer_prompted_output=True,
    )

    assert model == "anthropic:claude-sonnet-4-6"
