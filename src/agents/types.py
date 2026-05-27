"""Shared Pydantic output models for all agents.

These models serve double duty:
1. pydantic-ai validates LLM output against them (structured output).
2. Their .model_dump() is stored as Report.content in Postgres.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentOutputModel(BaseModel):
    """Base class for Anthropic-compatible structured outputs."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


class CategorySuggestion(AgentOutputModel):
    transaction_id: str
    suggested_category_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    # True when the positive-amount transaction reverses a prior expense
    # (Krankenkassen-Erstattung, Splitwise-Ausgleich, Spesen, Amazon-Refund).
    # Refund-flagged Tx use the *original* expense category so budgets net
    # automatically. False for genuine income (Gehalt, Steuerrückzahlung,
    # Cashback) and for normal expenses.
    is_refund: bool = False


class CategorizationResult(AgentOutputModel):
    suggestions: list[CategorySuggestion]
    # The counts are derived by the orchestrator from the actual data and
    # the configured threshold — the LLM doesn't need to fill them. Default
    # to 0 so newer/stricter models (Sonnet 4.6) don't fail validation when
    # they return only the `suggestions` list.
    uncategorized_count: int = 0
    high_confidence_count: int = Field(
        default=0,
        description="Number of suggestions with confidence >= the auto-apply threshold",
    )


# ---------------------------------------------------------------------------
# Shared observation (atomic unit for analysis/anomaly/synthesis)
# ---------------------------------------------------------------------------


class ObservationMetric(AgentOutputModel):
    key: str = Field(description="Short metric key, e.g. amount_delta or category_total")
    value: str = Field(description="Scalar metric value serialized as a short string")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)


class Observation(AgentOutputModel):
    category: str = Field(description="e.g. spending_trend, anomaly, new_counterparty")
    summary: str = Field(description="One-sentence human-readable observation")
    severity: str = Field(description="info, warning, or alert")
    transaction_ids: list[str] = Field(default_factory=list)
    metrics: list[ObservationMetric] = Field(default_factory=list)

    @field_validator("metrics", mode="before")
    @classmethod
    def _coerce_legacy_metrics(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        metrics: list[dict[str, str]] = []
        for key, metric_value in value.items():
            if isinstance(metric_value, str):
                scalar = metric_value
            else:
                scalar = json.dumps(metric_value, ensure_ascii=False)
            metrics.append({"key": str(key), "value": scalar})
        return metrics


# ---------------------------------------------------------------------------
# Analysis (weekly / monthly)
# ---------------------------------------------------------------------------


class AnalysisResult(AgentOutputModel):
    observations: list[Observation]
    period: str = Field(description="ISO period, e.g. 2026-W15 or 2026-03")
    summary_text: str = Field(description="2-3 sentence executive summary")


# ---------------------------------------------------------------------------
# Anomaly
# ---------------------------------------------------------------------------


class AnomalyResult(AgentOutputModel):
    anomalies: list[Observation]
    period: str
    total_anomalies: int
    new_counterparties: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class SynthesisResult(AgentOutputModel):
    executive_summary: str = Field(description="3-5 sentence weekly briefing")
    key_observations: list[Observation] = Field(description="Top 5 most important")
    action_items: list[str] = Field(default_factory=list)
    period: str
