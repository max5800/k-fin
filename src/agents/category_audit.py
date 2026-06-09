"""Category audit agent — flags category/evidence inconsistencies."""

from __future__ import annotations

import json
import logging
from datetime import date

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from sqlalchemy import Engine

from src.agents._anthropic import make_anthropic_model
from src.agents._runner import run_in_fresh_loop
from src.agents._usage import AgentUsage, extract_usage
from src.agents.context import get_safe_analysis_context
from src.agents.period import derive_period_label
from src.agents.prompts.category_audit import CATEGORY_AUDIT_SYSTEM_PROMPT
from src.agents.types import AnalysisResult
from src.services.llm_context import sanitize_context

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"
CATEGORY_AUDIT_MAX_TOKENS = 8000

category_audit_agent = Agent(
    make_anthropic_model(MODEL, prefer_prompted_output=True),
    output_type=AnalysisResult,
    system_prompt=CATEGORY_AUDIT_SYSTEM_PROMPT,
    retries=2,
    model_settings=ModelSettings(max_tokens=CATEGORY_AUDIT_MAX_TOKENS),
)


def run_category_audit(
    engine: Engine,
    reference_date: date | None = None,
    *,
    period_days: int | None = None,
    usage: AgentUsage | None = None,
) -> AnalysisResult:
    ref = reference_date or date.today()
    period, first_day, _last_day = derive_period_label("monthly", period_days, ref)

    context = get_safe_analysis_context(engine, year=first_day.year, month=first_day.month)

    safe_context = sanitize_context(context)
    prompt = (
        f"## Kategorie-Audit {period}\n\n"
        f"```json\n{json.dumps(safe_context, ensure_ascii=False, indent=2)}\n```\n\n"
        "Erstelle das Kategorie-Audit gemäß dem Schema."
    )
    result = run_in_fresh_loop(category_audit_agent.run(prompt))
    if usage is not None:
        in_t, out_t = extract_usage(result)
        usage.add_call(MODEL, in_t, out_t)
    return result.output
