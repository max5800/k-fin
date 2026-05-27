"""Tests for Anthropic model factory behaviour."""

from __future__ import annotations

import pytest
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel

from src.agents._anthropic import make_anthropic_model


def _prepared_agent_request(agent):
    output_toolset = agent._output_toolset  # pyright: ignore[reportPrivateUsage]
    output_schema = agent._output_schema  # pyright: ignore[reportPrivateUsage]
    params = ModelRequestParameters(
        function_tools=[],
        builtin_tools=[],
        output_mode=output_schema.mode,
        output_tools=output_toolset.__dict__["_tool_defs"],
        output_object=output_schema.object_def,
        prompted_output_template=None,
        allow_text_output=output_schema.allows_text,
        allow_image_output=output_schema.allows_image,
        instruction_parts=None,
    )
    return agent._model.prepare_request(agent.model_settings, params)  # pyright: ignore[reportPrivateUsage]


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


@pytest.mark.parametrize(
    "module_name,agent_name",
    [
        ("src.agents.weekly_analysis", "weekly_analysis_agent"),
        ("src.agents.monthly_analysis", "monthly_analysis_agent"),
        ("src.agents.anomaly", "anomaly_agent"),
        ("src.agents.synthesizer", "synthesizer_agent"),
    ],
)
def test_analysis_agents_do_not_prepare_native_output_config(module_name, agent_name):
    module = pytest.importorskip(module_name)
    agent = getattr(module, agent_name)

    model_settings, prepared = _prepared_agent_request(agent)

    assert prepared.output_mode == "prompted"
    assert agent._model._build_output_config(prepared, model_settings) is None  # pyright: ignore[reportPrivateUsage]
