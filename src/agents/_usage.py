"""Token usage + cost tracking for pydantic-ai agent runs.

The orchestrator persists per-run totals to `agent_runs` so we can
compare runs, spot regressions ("Haiku started using 2× tokens after
prompt change"), and estimate monthly spend.

Pricing is hardcoded per model — Anthropic doesn't expose a pricing
API. Update the PRICING dict when Anthropic changes rates or we
introduce a new model. Unknown models return cost=0 + warn-log so
logging never breaks the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


# Prices in USD per 1M tokens. Source: console.anthropic.com/pricing
# as of 2026-04. Keys match pydantic-ai's model identifiers.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "anthropic:claude-haiku-4-5-20251001": (Decimal("1.00"), Decimal("5.00")),
    "anthropic:claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "anthropic:claude-sonnet-4-20250514": (Decimal("3.00"), Decimal("15.00")),
    "anthropic:claude-sonnet-4-5-20250929": (Decimal("3.00"), Decimal("15.00")),
    "anthropic:claude-sonnet-4-5": (Decimal("3.00"), Decimal("15.00")),
    "anthropic:claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "anthropic:claude-opus-4-7": (Decimal("15.00"), Decimal("75.00")),
}


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Cost in USD for a single model call. Unknown model → 0 + warn."""
    price = PRICING.get(model)
    if price is None:
        logger.warning("No pricing entry for model %r — cost reported as 0", model)
        return Decimal("0")
    in_rate, out_rate = price
    return (Decimal(input_tokens) * in_rate + Decimal(output_tokens) * out_rate) / Decimal("1000000")


def extract_usage(result: Any) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from a pydantic-ai RunResult.

    pydantic-ai has renamed these fields across versions (`request_tokens`
    → `input_tokens`); probe for both so we survive upgrades.
    """
    try:
        usage = result.usage()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Failed to extract usage from agent result")
        return 0, 0
    in_t = getattr(usage, "input_tokens", None) or getattr(usage, "request_tokens", 0) or 0
    out_t = getattr(usage, "output_tokens", None) or getattr(usage, "response_tokens", 0) or 0
    return int(in_t), int(out_t)


@dataclass
class AgentUsage:
    """Accumulated token usage + cost across one or more LLM calls.

    `extra` carries free-form per-agent metrics (e.g. categorization's
    memory-hit stats). Stays per-agent — the cross-agent total intentionally
    drops it because aggregation rarely makes sense for these fields.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    extra: dict[str, Any] = field(default_factory=dict)

    def add_call(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += calc_cost(model, input_tokens, output_tokens)

    def merge(self, other: AgentUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


def usage_from_result(result: Any, model: str) -> AgentUsage:
    """Convenience: build an AgentUsage from a single agent-run result."""
    in_t, out_t = extract_usage(result)
    usage = AgentUsage()
    usage.add_call(model, in_t, out_t)
    return usage
