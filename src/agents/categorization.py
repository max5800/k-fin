"""Categorization agent — LLM-based classification for uncategorized tx.

Uses Sonnet 4.6 with the WebSearchTool so unknown local merchants
("Böhnlich Bamberg" → Fleischerei → Lebensmittel) can be looked up.
Processes pages of 200 in batches of 25, applies high-confidence
suggestions immediately, then loops until everything is classified
(safety-capped at MAX_TRANSACTIONS_PER_RUN).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from src.agents._runner import run_in_fresh_loop
from src.agents._usage import AgentUsage, extract_usage
from src.agents.gather import (
    count_uncategorized_transactions,
    get_available_categories,
    get_uncategorized_transactions,
)
from src.agents.prompts.categorization import CATEGORIZATION_SYSTEM_PROMPT
from src.agents.types import CategorizationResult, CategorySuggestion
from src.core.db.models import NormalizedTransaction

logger = logging.getLogger(__name__)

# Tuned for Sonnet 4.6 + WebSearchTool throughput. Larger batches reduce
# the per-batch fixed overhead (system prompt + tool wiring); concurrency
# overlaps web-search latency across batches. Anthropic Tier-1 rate
# limits (50+ RPM for Sonnet) easily absorb 3 concurrent batches.
BATCH_SIZE = 50
PAGE_SIZE = 200
MAX_CONCURRENT_BATCHES = 3
MAX_TRANSACTIONS_PER_RUN = 5000

DEFAULT_AUTO_APPLY_CONFIDENCE = 0.6

MODEL = "anthropic:claude-sonnet-4-6"

categorization_agent = Agent(
    # Sonnet 4.6 after an A/B vs Haiku 4.5 on 26 low-confidence tx:
    # Haiku auto-applied 77%, Sonnet 100%. Sonnet has the domain
    # knowledge Haiku lacks — it recognises "LOGPAY" as a German gas-
    # station payment processor, Mandatsref+high-amount lastschrifts as
    # rent, etc. Haiku fell back to a generic "elektronik @0.50" guess
    # on those. Scaled to a full 479-tx run, Sonnet cuts the review
    # queue from ~77 items to ~0-15 for +$0.88 cost (~$1.32 vs $0.44).
    #
    # max_tokens=16000: pydantic-ai's Anthropic backend defaults to
    # 4096. A 50-tx batch yields ~5–6k output tokens (each suggestion
    # is a ~110-token JSON object with German reasoning). Hitting 4096
    # mid-JSON makes Anthropic surface a truncated tool_use with
    # args={}, which then fails CategorizationResult validation and
    # burns all retries. 16k gives comfortable 3× headroom.
    MODEL,
    output_type=CategorizationResult,
    system_prompt=CATEGORIZATION_SYSTEM_PROMPT,
    retries=2,
    model_settings=ModelSettings(max_tokens=16000),
)


ProgressCallback = Callable[[int, int, str], None]


def _format_user_prompt(transactions: list[dict], categories: list[dict]) -> str:
    """Build the user prompt with actual data."""
    cat_lines = "\n".join(f"- {c['id']}: {c['name']} ({c['type']})" for c in categories)
    tx_lines = json.dumps(transactions, ensure_ascii=False, indent=2)
    return (
        f"## Verfügbare Kategorien\n\n{cat_lines}\n\n"
        f"## Unkategorisierte Transaktionen\n\n{tx_lines}\n\n"
        "Ordne jeder Transaktion eine Kategorie zu. "
        "Antworte als JSON gemäß dem Schema."
    )


def apply_high_confidence(
    engine: Engine,
    suggestions: list[CategorySuggestion],
    threshold: float,
) -> int:
    """Auto-apply suggestions whose confidence >= threshold.

    Idempotent: only updates rows where category_id IS NULL, so a re-apply
    won't overwrite manual corrections.
    """
    to_apply = [s for s in suggestions if s.confidence >= threshold]
    if not to_apply:
        return 0

    applied = 0
    with Session(engine) as session:
        for s in to_apply:
            applied += session.execute(
                update(NormalizedTransaction)
                .where(NormalizedTransaction.id == s.transaction_id)
                .where(NormalizedTransaction.category_id.is_(None))
                .values(category_id=s.suggested_category_id)
            ).rowcount
        session.commit()
    return applied


def run_categorization(
    engine: Engine,
    *,
    on_progress: ProgressCallback | None = None,
    auto_apply_threshold: float = DEFAULT_AUTO_APPLY_CONFIDENCE,
    usage: AgentUsage | None = None,
) -> CategorizationResult:
    """Categorize ALL uncategorized transactions, in pages of PAGE_SIZE.

    Applies high-confidence suggestions inline so the next page-fetch sees
    a shrinking working set. Capped at MAX_TRANSACTIONS_PER_RUN to guard
    against pathological loops (LLM returns garbage IDs, DB glitches, …).
    """
    categories = get_available_categories(engine)
    if not categories:
        logger.warning("No categories defined — skipping categorization")
        return CategorizationResult(
            suggestions=[], uncategorized_count=0, high_confidence_count=0
        )

    initial_total = count_uncategorized_transactions(engine)
    if initial_total == 0:
        logger.info("No uncategorized transactions — skipping LLM call")
        if on_progress:
            on_progress(0, 0, "Keine offenen Transaktionen")
        return CategorizationResult(
            suggestions=[], uncategorized_count=0, high_confidence_count=0
        )

    if on_progress:
        on_progress(0, initial_total, "Kategorisierung startet…")

    all_suggestions: list[CategorySuggestion] = []
    total_applied = 0
    processed = 0
    page_num = 0

    def _run_batch(batch: list[dict]) -> tuple[list[CategorySuggestion], int, int]:
        """Run one LLM batch + apply its high-confidence suggestions inline."""
        prompt = _format_user_prompt(batch, categories)
        result = run_in_fresh_loop(categorization_agent.run(prompt))
        if usage is not None:
            in_t, out_t = extract_usage(result)
            usage.add_call(MODEL, in_t, out_t)
        suggestions = list(result.output.suggestions)
        applied = apply_high_confidence(engine, suggestions, auto_apply_threshold)
        return suggestions, applied, len(batch)

    while processed < MAX_TRANSACTIONS_PER_RUN:
        page = get_uncategorized_transactions(engine, limit=PAGE_SIZE)
        if not page:
            break
        page_num += 1
        batches = [page[i : i + BATCH_SIZE] for i in range(0, len(page), BATCH_SIZE)]
        logger.info(
            "Categorization page %d: %d transactions, %d batches × %d, "
            "%d concurrent (processed %d so far)",
            page_num,
            len(page),
            len(batches),
            BATCH_SIZE,
            MAX_CONCURRENT_BATCHES,
            processed,
        )

        page_failed = 0
        page_succeeded = 0
        page_applied = 0
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BATCHES) as ex:
            futures = {ex.submit(_run_batch, b): idx for idx, b in enumerate(batches)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    suggestions, applied, batch_len = fut.result()
                except Exception:
                    page_failed += 1
                    logger.exception("Batch %d failed", idx + 1)
                    continue
                page_succeeded += 1
                page_applied += applied
                all_suggestions.extend(suggestions)
                total_applied += applied
                processed += batch_len
                logger.info(
                    "  batch %d/%d done — %d suggestions, %d applied (%d/%d total)",
                    idx + 1,
                    len(batches),
                    len(suggestions),
                    applied,
                    processed,
                    initial_total,
                )
                if on_progress:
                    display_current = min(processed, initial_total)
                    on_progress(
                        display_current,
                        initial_total,
                        f"Kategorisiere {display_current}/{initial_total}",
                    )
                if processed >= MAX_TRANSACTIONS_PER_RUN:
                    logger.warning(
                        "Categorization safety cap reached at %d transactions",
                        MAX_TRANSACTIONS_PER_RUN,
                    )
                    break

        # Safety: if every batch in this page failed, the next page-fetch
        # would return the same rows (nothing got applied) → infinite loop.
        # Bail out so the run finishes as failed rather than hanging.
        if page_succeeded == 0 and page_failed > 0:
            raise RuntimeError(
                f"Categorization page {page_num} fully failed "
                f"({page_failed} batches) — aborting to avoid infinite retry"
            )

        # Same loop risk: batches succeeded but zero suggestions met the
        # auto-apply threshold. The next page-fetch returns the same rows
        # (still NULL category_id) and we'd re-invoke the LLM forever.
        # Break out — the low-confidence suggestions that did come back
        # are already in `all_suggestions` for the caller to show in Review.
        if page_succeeded > 0 and page_applied == 0:
            logger.warning(
                "Categorization page %d: %d batches succeeded but no suggestions "
                "met auto-apply threshold (%.2f) — stopping to avoid re-processing "
                "the same %d transactions",
                page_num,
                page_succeeded,
                auto_apply_threshold,
                len(page),
            )
            break

    # Dedupe per transaction_id — keep the highest-confidence suggestion.
    # The page-loop + occasional LLM hedging can produce 2-5 entries for the
    # same tx; downstream consumers (Review UI) would show them as duplicates.
    best_by_tx: dict[str, CategorySuggestion] = {}
    for s in all_suggestions:
        prev = best_by_tx.get(s.transaction_id)
        if prev is None or s.confidence > prev.confidence:
            best_by_tx[s.transaction_id] = s
    deduped = list(best_by_tx.values())
    if len(deduped) != len(all_suggestions):
        logger.info(
            "Deduped suggestions: %d → %d (removed %d duplicates per tx_id)",
            len(all_suggestions),
            len(deduped),
            len(all_suggestions) - len(deduped),
        )

    high_conf = sum(1 for s in deduped if s.confidence >= auto_apply_threshold)
    logger.info(
        "Categorization done: %d processed, %d unique suggestions, %d auto-applied",
        processed,
        len(deduped),
        total_applied,
    )
    return CategorizationResult(
        suggestions=deduped,
        uncategorized_count=initial_total,
        high_confidence_count=high_conf,
    )
