"""Anomaly detection agent — flags unusual transactions."""

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
    get_new_counterparties,
    get_outlier_transactions,
    get_period_transactions,
    get_recent_reports,
)
from src.agents.period import derive_period_label
from src.agents.prompts.anomaly import ANOMALY_SYSTEM_PROMPT
from src.agents.types import AnomalyResult

logger = logging.getLogger(__name__)

MODEL = "anthropic:claude-sonnet-4-6"
ANOMALY_MAX_TOKENS = 8000

anomaly_agent = Agent(
    make_anthropic_model(MODEL, prefer_native_output=True),
    output_type=AnomalyResult,
    system_prompt=ANOMALY_SYSTEM_PROMPT,
    retries=2,
    model_settings=ModelSettings(max_tokens=ANOMALY_MAX_TOKENS),
)


def run_anomaly_detection(
    engine: Engine,
    reference_date: date | None = None,
    lookback_days: int = 30,
    *,
    period_days: int | None = None,
    usage: AgentUsage | None = None,
) -> AnomalyResult:
    """Gather anomaly-relevant data and run the detection agent.

    ``period_days`` is the API-facing override: when set it replaces the
    default ``lookback_days``. The dual naming keeps existing callers
    (orchestrator, scheduler) source-compatible.
    """
    if period_days is not None and period_days > 0:
        lookback_days = period_days
    ref = reference_date or date.today()
    # `period_days` switches the label to the canonical date-range form
    # (so report keys stay distinct); a default 30-day call keeps the
    # legacy date-range label too — anomaly never emitted "YYYY-MM" or
    # "YYYY-Www" so the format is identical either way.
    period, date_from, _ = derive_period_label(
        "anomaly", period_days, ref
    )

    outliers = get_outlier_transactions(engine, date_from, ref)
    new_counterparties = get_new_counterparties(engine, date_from)
    transactions = get_period_transactions(engine, date_from, ref)

    # No early-return on empty signals — the agent runs even when the
    # statistical pre-filter is quiet so it can still flag subtle patterns
    # (missing recurring expenses, unusual gaps, etc.). The system prompt
    # is instructed to return an empty result instead of hallucinating.
    if not outliers and not new_counterparties:
        logger.info(
            "No hard outliers/new counterparties for period %s — running "
            "anomaly agent on full transaction sample",
            period,
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
