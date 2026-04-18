"""Shared Pydantic output models for all agents.

These models serve double duty:
1. pydantic-ai validates LLM output against them (structured output).
2. Their .model_dump() is stored as Report.content in Postgres.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


class CategorySuggestion(BaseModel):
    transaction_id: str
    suggested_category_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class CategorizationResult(BaseModel):
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


class Observation(BaseModel):
    category: str = Field(description="e.g. spending_trend, anomaly, new_counterparty")
    summary: str = Field(description="One-sentence human-readable observation")
    severity: str = Field(description="info, warning, or alert")
    transaction_ids: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Analysis (weekly / monthly)
# ---------------------------------------------------------------------------


class AnalysisResult(BaseModel):
    observations: list[Observation]
    period: str = Field(description="ISO period, e.g. 2026-W15 or 2026-03")
    summary_text: str = Field(description="2-3 sentence executive summary")


# ---------------------------------------------------------------------------
# Anomaly
# ---------------------------------------------------------------------------


class AnomalyResult(BaseModel):
    anomalies: list[Observation]
    period: str
    total_anomalies: int
    new_counterparties: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class SynthesisResult(BaseModel):
    executive_summary: str = Field(description="3-5 sentence weekly briefing")
    key_observations: list[Observation] = Field(description="Top 5 most important")
    action_items: list[str] = Field(default_factory=list)
    period: str
