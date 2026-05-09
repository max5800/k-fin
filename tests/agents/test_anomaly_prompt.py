"""Tests for the anomaly-agent system prompt.

The full agent run is exercised in `test_anomaly.py` against a stubbed
LLM. This file pins prompt-level invariants — i.e. guarantees that
must hold regardless of the model — by asserting on the prompt string
itself.

Why pin these in tests at all? The prompt drives behaviour the user
actually sees in the daily/weekly anomaly report. A drive-by edit
that drops the `is_refund=true` damper would silently flip
Krankenkassen-Erstattungen back into "ungewöhnlich hohes Einkommen"
warnings, which is exactly the noise we removed.
"""

from __future__ import annotations

from src.agents.prompts.anomaly import ANOMALY_SYSTEM_PROMPT


def test_prompt_dampens_refunds_in_income_anomalies():
    """A refund (is_refund=true) must NOT be flagged as income
    anomaly. Pin the wording so future edits don't silently regress.
    """
    text = ANOMALY_SYSTEM_PROMPT.lower()
    assert "is_refund" in text, (
        "Prompt must reference the is_refund field by name so the "
        "model recognises the flag in transaction payloads."
    )
    # Two separate signals: the field name AND the explicit "don't flag
    # as income anomaly" guidance.
    assert "neutralisiert" in text or "nicht als" in text, (
        "Prompt must explicitly tell the model NOT to treat refunds "
        "as income anomalies."
    )
    assert "einkommens" in text, (
        "Prompt must connect the refund damper to the income-anomaly "
        "category specifically (not generic 'ignore positive amounts')."
    )


def test_prompt_still_promotes_low_false_positive_rate():
    """The refund damper sits next to the existing 'minimise false
    positives' rule. Sanity-check that we didn't accidentally drop the
    surrounding guidance.
    """
    assert "Falsch-Positive minimieren" in ANOMALY_SYSTEM_PROMPT


def test_prompt_keeps_empty_result_path():
    """Empty outlier_transactions + empty new_counterparties is a valid
    output. Ensure the empty-result guidance survived prompt edits.
    """
    assert "Leere outlier_transactions" in ANOMALY_SYSTEM_PROMPT
    assert "total_anomalies=0" in ANOMALY_SYSTEM_PROMPT
