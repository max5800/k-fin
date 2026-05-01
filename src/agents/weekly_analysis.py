"""Weekly analysis agent — interprets this week's financial aggregates."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from pydantic_ai import Agent
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
from src.agents.prompts.weekly_analysis import WEEKLY_ANALYSIS_SYSTEM_PROMPT
from src.agents.types import AnalysisResult

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"

weekly_analysis_agent = Agent(
    make_anthropic_model(MODEL),
    output_type=AnalysisResult,
    system_prompt=WEEKLY_ANALYSIS_SYSTEM_PROMPT,
    retries=2,
)


def _current_week_range(reference: date | None = None) -> tuple[date, date]:
    """Return (monday, sunday) of the current or given week."""
    ref = reference or date.today()
    monday = ref - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def run_weekly_analysis(
    engine: Engine,
    reference_date: date | None = None,
    *,
    usage: AgentUsage | None = None,
) -> AnalysisResult:
    """Gather weekly data and run the analysis agent."""
    monday, sunday = _current_week_range(reference_date)
    iso_week = monday.strftime("%G-W%V")

    transactions = get_period_transactions(engine, monday, sunday)
    if not transactions:
        logger.info("No transactions for week %s — skipping", iso_week)
        return AnalysisResult(
            observations=[], period=iso_week, summary_text="Keine Transaktionen in dieser Woche."
        )

    monthly = get_monthly_summary(engine, months=3)
    categories = get_category_breakdown(engine, monday, sunday)
    recurring = get_recurring_patterns(engine)
    savings = get_savings_rate(engine, monday, sunday)
    memory = get_recent_reports(engine, "weekly_analysis", limit=2)

    data = {
        "period": iso_week,
        "date_range": f"{monday.isoformat()} bis {sunday.isoformat()}",
        "transactions_count": len(transactions),
        "transactions": transactions[:50],  # cap for prompt size
        "monthly_trend": monthly,
        "category_breakdown": categories,
        "recurring_patterns": recurring,
        "savings_rate": savings,
    }
    prompt_parts = [
        f"## Wochenanalyse {iso_week}\n",
        f"Zeitraum: {data['date_range']}\n",
        f"### Daten\n\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n",
    ]
    if memory:
        prompt_parts.append(
            f"### Vorherige Analysen (Kontext)\n\n"
            f"```json\n{json.dumps(memory, ensure_ascii=False, indent=2)}\n```\n"
        )
    prompt_parts.append("Erstelle die Wochenanalyse gemäß dem Schema.")

    result = run_in_fresh_loop(weekly_analysis_agent.run("\n".join(prompt_parts)))
    if usage is not None:
        in_t, out_t = extract_usage(result)
        usage.add_call(MODEL, in_t, out_t)
    return result.output
