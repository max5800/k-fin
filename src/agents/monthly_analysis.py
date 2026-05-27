"""Monthly analysis agent — deep dive into monthly financial trends."""

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
from src.agents.gather import (
    get_category_breakdown,
    get_monthly_summary,
    get_period_transactions,
    get_recent_reports,
    get_recurring_patterns,
    get_savings_rate,
)
from src.agents.period import derive_period_label
from src.agents.prompts.monthly_analysis import MONTHLY_ANALYSIS_SYSTEM_PROMPT
from src.agents.types import AnalysisResult

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"
MONTHLY_ANALYSIS_MAX_TOKENS = 8000

monthly_analysis_agent = Agent(
    make_anthropic_model(MODEL, prefer_prompted_output=True),
    output_type=AnalysisResult,
    system_prompt=MONTHLY_ANALYSIS_SYSTEM_PROMPT,
    retries=2,
    model_settings=ModelSettings(max_tokens=MONTHLY_ANALYSIS_MAX_TOKENS),
)


def run_monthly_analysis(
    engine: Engine,
    reference_date: date | None = None,
    *,
    period_days: int | None = None,
    usage: AgentUsage | None = None,
) -> AnalysisResult:
    """Gather monthly data and run the analysis agent.

    By default the agent inspects the previous complete calendar month.
    When ``period_days`` is set, it instead inspects the last N days
    ending at ``reference_date`` (or today). The ``period`` label drops
    to an ISO date range (``YYYY-MM-DD/YYYY-MM-DD``) for non-month
    windows so downstream report keys stay distinct.
    """
    ref = reference_date or date.today()
    period, first_day, last_day = derive_period_label("monthly", period_days, ref)

    transactions = get_period_transactions(engine, first_day, last_day)
    if not transactions:
        logger.info("No transactions for month %s — skipping", period)
        return AnalysisResult(
            observations=[], period=period, summary_text="Keine Transaktionen in diesem Monat."
        )

    monthly = get_monthly_summary(engine, months=6)
    categories = get_category_breakdown(engine, first_day, last_day)
    recurring = get_recurring_patterns(engine)
    savings = get_savings_rate(engine, first_day, last_day)
    memory = get_recent_reports(engine, "monthly_analysis", limit=2)

    data = {
        "period": period,
        "date_range": f"{first_day.isoformat()} bis {last_day.isoformat()}",
        "transactions_count": len(transactions),
        "transactions": transactions[:100],
        "monthly_trend_6m": monthly,
        "category_breakdown": categories,
        "recurring_patterns": recurring,
        "savings_rate": savings,
    }
    prompt_parts = [
        f"## Monatsanalyse {period}\n",
        f"Zeitraum: {data['date_range']}\n",
        f"### Daten\n\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n",
    ]
    if memory:
        prompt_parts.append(
            f"### Vorherige Monatsanalysen (Kontext)\n\n"
            f"```json\n{json.dumps(memory, ensure_ascii=False, indent=2)}\n```\n"
        )
    prompt_parts.append("Erstelle die Monatsanalyse gemäß dem Schema.")

    result = run_in_fresh_loop(monthly_analysis_agent.run("\n".join(prompt_parts)))
    if usage is not None:
        in_t, out_t = extract_usage(result)
        usage.add_call(MODEL, in_t, out_t)
    return result.output
