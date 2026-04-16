"""Monthly analysis agent — deep dive into monthly financial trends."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from pydantic_ai import Agent
from sqlalchemy import Engine

from src.agents.gather import (
    get_category_breakdown,
    get_monthly_summary,
    get_period_transactions,
    get_recent_reports,
    get_recurring_patterns,
    get_savings_rate,
)
from src.agents.prompts.monthly_analysis import MONTHLY_ANALYSIS_SYSTEM_PROMPT
from src.agents.types import AnalysisResult

logger = logging.getLogger(__name__)

monthly_analysis_agent = Agent(
    "anthropic:claude-sonnet-4-20250514",
    output_type=AnalysisResult,
    system_prompt=MONTHLY_ANALYSIS_SYSTEM_PROMPT,
    retries=2,
)


def _current_month_range(reference: date | None = None) -> tuple[date, date]:
    """Return (first_day, last_day) of the previous complete month."""
    ref = reference or date.today()
    first_of_this_month = ref.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev, last_of_prev


def run_monthly_analysis(
    engine: Engine,
    reference_date: date | None = None,
) -> AnalysisResult:
    """Gather monthly data and run the analysis agent."""
    first_day, last_day = _current_month_range(reference_date)
    period = first_day.strftime("%Y-%m")

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

    result = monthly_analysis_agent.run_sync("\n".join(prompt_parts))
    return result.output
