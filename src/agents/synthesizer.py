"""Synthesizer agent — combines all agent outputs into a weekly report."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from sqlalchemy import Engine

from src.agents._anthropic import make_anthropic_model
from src.agents._runner import run_in_fresh_loop
from src.agents._usage import AgentUsage, extract_usage
from src.agents.gather import get_latest_report_content, get_recent_reports
from src.agents.prompts.synthesizer import SYNTHESIZER_SYSTEM_PROMPT
from src.agents.types import (
    AnalysisResult,
    AnomalyResult,
    CategorizationResult,
    SynthesisResult,
)

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"
SYNTHESIZER_MAX_TOKENS = 8000

synthesizer_agent = Agent(
    make_anthropic_model(MODEL, prefer_native_output=True),
    output_type=SynthesisResult,
    system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
    retries=2,
    model_settings=ModelSettings(max_tokens=SYNTHESIZER_MAX_TOKENS),
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
    """Combine all agent results into a synthesis report.

    When invoked standalone (no in-memory results from a sibling run), the
    function rehydrates inputs from the most recent persisted reports of
    each type. Without this fallback the solo `Run Synthese`-button on the
    Agents page produced an empty `"Keine Agent-Ergebnisse"`-result with
    no LLM call.
    """
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

    # Solo-run rehydration: pull the latest persisted report of each type.
    # Each lookup is independent — if e.g. anomaly never ran we still
    # synthesize from whatever else is available.
    if len(inputs) == 1:
        for key, report_type in (
            ("categorization", "categorization"),
            ("weekly_analysis", "weekly_analysis"),
            ("monthly_analysis", "monthly_analysis"),
            ("anomaly_detection", "anomaly"),
        ):
            content = get_latest_report_content(engine, report_type)
            if content is not None:
                inputs[key] = content
        if len(inputs) > 1:
            logger.info(
                "Synthesizer solo-run: rehydrated %d prior report(s) from DB",
                len(inputs) - 1,
            )

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
