"""Categorization agent — LLM fallback for rule-engine gaps.

Uses Haiku 4.5 (fast, cheap) to classify uncategorized transactions.
High-confidence suggestions can be auto-applied; low-confidence ones
are surfaced for review.
"""

from __future__ import annotations

import json
import logging

from pydantic_ai import Agent
from sqlalchemy import Engine

from src.agents.gather import get_available_categories, get_uncategorized_transactions
from src.agents.prompts.categorization import CATEGORIZATION_SYSTEM_PROMPT
from src.agents.types import CategorizationResult

logger = logging.getLogger(__name__)

categorization_agent = Agent(
    "anthropic:claude-haiku-4-5-20250514",
    output_type=CategorizationResult,
    system_prompt=CATEGORIZATION_SYSTEM_PROMPT,
    retries=2,
)


def _format_user_prompt(
    transactions: list[dict], categories: list[dict]
) -> str:
    """Build the user prompt with actual data."""
    cat_lines = "\n".join(
        f"- {c['id']}: {c['name']} ({c['type']})" for c in categories
    )
    tx_lines = json.dumps(transactions, ensure_ascii=False, indent=2)
    return (
        f"## Verfügbare Kategorien\n\n{cat_lines}\n\n"
        f"## Unkategorisierte Transaktionen\n\n{tx_lines}\n\n"
        "Ordne jeder Transaktion eine Kategorie zu. "
        "Antworte als JSON gemäß dem Schema."
    )


def run_categorization(
    engine: Engine,
) -> CategorizationResult:
    """Gather data and run the categorization agent.

    Returns a CategorizationResult.  If there are no uncategorized
    transactions, returns an empty result without calling the LLM.
    """
    uncategorized = get_uncategorized_transactions(engine, limit=200)
    if not uncategorized:
        logger.info("No uncategorized transactions — skipping LLM call")
        return CategorizationResult(
            suggestions=[], uncategorized_count=0, high_confidence_count=0
        )

    categories = get_available_categories(engine)
    if not categories:
        logger.warning("No categories defined — skipping categorization")
        return CategorizationResult(
            suggestions=[],
            uncategorized_count=len(uncategorized),
            high_confidence_count=0,
        )

    prompt = _format_user_prompt(uncategorized, categories)
    result = categorization_agent.run_sync(prompt)
    return result.output
