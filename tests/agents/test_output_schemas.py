"""Structured-output schema guarantees for agent result models."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from src.agents.types import (
    AnalysisResult,
    AnomalyResult,
    CategorizationResult,
    Observation,
    SynthesisResult,
)


def _walk_json_schema(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_schema(child)


def test_agent_output_schemas_are_anthropic_strict_objects():
    output_models = [
        CategorizationResult,
        AnalysisResult,
        AnomalyResult,
        SynthesisResult,
    ]

    for model in output_models:
        schema = model.model_json_schema()
        object_schemas = [
            item for item in _walk_json_schema(schema) if item.get("type") == "object"
        ]

        assert object_schemas
        assert all(
            item.get("additionalProperties") is False for item in object_schemas
        )


def test_observation_accepts_legacy_metrics_object():
    observation = Observation.model_validate(
        {
            "category": "spending_trend",
            "summary": "Lebensmittel sind gestiegen.",
            "severity": "warning",
            "transaction_ids": [],
            "metrics": {"delta_eur": 42.5, "note": "gegen Vormonat"},
        }
    )

    assert observation.metrics[0].key == "delta_eur"
    assert observation.metrics[0].value == "42.5"
    assert observation.metrics[1].key == "note"
    assert observation.metrics[1].value == "gegen Vormonat"
