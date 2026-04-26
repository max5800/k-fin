"""Synthesizer agent — combines all agent outputs into a weekly report."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from pydantic_ai import Agent
from sqlalchemy import Engine

from src.agents._runner import run_in_fresh_loop
from src.agents._usage import AgentUsage, extract_usage
from src.agents.gather import get_recent_reports
from src.agents.prompts.synthesizer import SYNTHESIZER_SYSTEM_PROMPT
from src.agents.types import (
    AnalysisResult,
    AnomalyResult,
    CategorizationResult,
    SynthesisResult,
)

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"

synthesizer_agent = Agent(
    MODEL,
    output_type=SynthesisResult,
    system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
    retries=2,
)


def run_synthesizer(
    engine: Engine,
    categorization: CategorizationResult | None = None,
    weekly: AnalysisResult | None = None,
    monthly: AnalysisResult | None = None,
    anomaly: AnomalyResult | None = None,
    reference_date: date | None = None,
    *,
    usage: AgentUsage | None = None,
) -> SynthesisResult:
    """Combine all agent results into a synthesis report."""
    ref = reference_date or date.today()
    monday = ref - timedelta(days=ref.weekday())
    period = monday.strftime("%G-W%V")

    inputs: dict = {"period": period}
    if categorization:
        inputs["categorization"] = categorization.model_dump()
    if weekly:
        inputs["weekly_analysis"] = weekly.model_dump()
    if monthly:
        inputs["monthly_analysis"] = monthly.model_dump()
    if anomaly:
        inputs["anomaly_detection"] = anomaly.model_dump()

    if len(inputs) <= 1:
        logger.info("No agent results to synthesize — returning empty")
        return SynthesisResult(
            executive_summary="Keine Agent-Ergebnisse für diese Woche verfügbar.",
            key_observations=[],
            action_items=[],
            period=period,
        )

    memory = get_recent_reports(engine, "synthesis", limit=2)

    prompt_parts = [
        f"## Wochenbericht-Synthese {period}\n",
        f"### Agent-Ergebnisse\n\n```json\n{json.dumps(inputs, ensure_ascii=False, indent=2)}\n```\n",
    ]
    if memory:
        prompt_parts.append(
            f"### Vorherige Synthesen (Kontext)\n\n"
            f"```json\n{json.dumps(memory, ensure_ascii=False, indent=2)}\n```\n"
        )
    prompt_parts.append("Erstelle den Wochenbericht gemäß dem Schema.")

    result = run_in_fresh_loop(synthesizer_agent.run("\n".join(prompt_parts)))
    if usage is not None:
        in_t, out_t = extract_usage(result)
        usage.add_call(MODEL, in_t, out_t)
    return result.output
