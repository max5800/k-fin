"""Anomaly detection agent — flags unusual transactions."""

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
    get_new_counterparties,
    get_outlier_transactions,
    get_period_transactions,
    get_recent_reports,
)
from src.agents.prompts.anomaly import ANOMALY_SYSTEM_PROMPT
from src.agents.types import AnomalyResult

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"

anomaly_agent = Agent(
    make_anthropic_model(MODEL),
    output_type=AnomalyResult,
    system_prompt=ANOMALY_SYSTEM_PROMPT,
    retries=2,
)


def run_anomaly_detection(
    engine: Engine,
    reference_date: date | None = None,
    lookback_days: int = 30,
    *,
    usage: AgentUsage | None = None,
) -> AnomalyResult:
    """Gather anomaly-relevant data and run the detection agent."""
    ref = reference_date or date.today()
    date_from = ref - timedelta(days=lookback_days)
    period = f"{date_from.isoformat()}/{ref.isoformat()}"

    outliers = get_outlier_transactions(engine, date_from, ref)
    new_counterparties = get_new_counterparties(engine, date_from)
    transactions = get_period_transactions(engine, date_from, ref)

    if not outliers and not new_counterparties:
        logger.info("No anomalies detected for period %s — skipping", period)
        return AnomalyResult(
            anomalies=[],
            period=period,
            total_anomalies=0,
            new_counterparties=[],
        )

    memory = get_recent_reports(engine, "anomaly", limit=2)

    data = {
        "period": period,
        "lookback_days": lookback_days,
        "total_transactions": len(transactions),
        "outlier_transactions": outliers,
        "new_counterparties": new_counterparties,
        "recent_transactions_sample": transactions[:30],
    }
    prompt_parts = [
        f"## Anomalie-Erkennung {period}\n",
        f"Analysezeitraum: {lookback_days} Tage\n",
        f"### Daten\n\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n",
    ]
    if memory:
        prompt_parts.append(
            f"### Vorherige Anomalie-Reports (Kontext)\n\n"
            f"```json\n{json.dumps(memory, ensure_ascii=False, indent=2)}\n```\n"
        )
    prompt_parts.append("Bewerte die Anomalien gemäß dem Schema.")

    result = run_in_fresh_loop(anomaly_agent.run("\n".join(prompt_parts)))
    if usage is not None:
        in_t, out_t = extract_usage(result)
        usage.add_call(MODEL, in_t, out_t)
    return result.output
