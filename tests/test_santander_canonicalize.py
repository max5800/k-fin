"""Santander canonical-adapter tests (M16-P2c).

Covers the projection onto the shared canonical dict, FX preservation
(USD / GBP), and the deterministic-id fallback for transactions Santander
gave no stable id.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.normalization.canonicalize import CANONICAL_FIELDS_FOR_HASH, content_hash
from src.normalization.santander_canonicalize import (
    derive_external_id,
    santander_canonicalize,
)
from src.external.santander_models import SantanderTransaction

_FIXTURES = Path(__file__).parent / "fixtures"


def _txs() -> list[dict]:
    raw = json.loads((_FIXTURES / "santander_cc_transactions.json").read_text())
    return raw["transactions_response"]["transactions"]


def test_eur_purchase_maps_to_recipient_no_fx():
    canon = santander_canonicalize(_txs()[0])
    assert canon["external_id"] == "TEST-TX-EUR"
    assert canon["booking_date"] == date(2026, 5, 4)
    assert canon["valuation_date"] == date(2026, 5, 5)
    assert canon["amount"] == Decimal("-42.50")
    assert canon["currency"] == "EUR"
    # debit → merchant is the recipient, sender is None, no IBANs.
    assert canon["recipient"] == "REWE Markt"
    assert canon["sender"] is None
    assert canon["sender_iban"] is None and canon["recipient_iban"] is None
    # EUR transaction → no FX leg.
    assert canon["original_amount"] is None
    assert canon["original_currency"] is None


def test_usd_purchase_preserves_fx():
    canon = santander_canonicalize(_txs()[1])
    assert canon["amount"] == Decimal("-91.80")
    assert canon["currency"] == "EUR"
    assert canon["original_amount"] == Decimal("-99.00")
    assert canon["original_currency"] == "USD"


def test_gbp_purchase_preserves_fx():
    canon = santander_canonicalize(_txs()[2])
    assert canon["original_amount"] == Decimal("-49.00")
    assert canon["original_currency"] == "GBP"


def test_missing_id_gets_deterministic_hash():
    pending = _txs()[3]
    canon = santander_canonicalize(pending)
    ext = canon["external_id"]
    assert ext.startswith("santander-")
    # Deterministic — re-canonicalizing the same payload yields the same id.
    assert santander_canonicalize(pending)["external_id"] == ext


def test_derived_id_differs_for_distinct_purchases():
    base = _txs()[3]
    other = {**base, "amount": "-8.40"}
    assert (
        derive_external_id(SantanderTransaction.model_validate(base))
        != derive_external_id(SantanderTransaction.model_validate(other))
    )


def test_canonical_has_all_hash_fields():
    """The adapter output must carry every content-hash identity field."""
    canon = santander_canonicalize(_txs()[0])
    assert set(CANONICAL_FIELDS_FOR_HASH) <= set(canon)
    # content_hash is stable across two runs of the same payload.
    assert content_hash(canon, source="santander_cc") == content_hash(
        santander_canonicalize(_txs()[0]), source="santander_cc"
    )


def test_credit_maps_merchant_to_sender():
    """A positive amount (refund) → merchant is the sender."""
    refund = {**_txs()[0], "id": "TEST-TX-REFUND", "amount": "12.00"}
    canon = santander_canonicalize(refund)
    assert canon["sender"] == "REWE Markt"
    assert canon["recipient"] is None
