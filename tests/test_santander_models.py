"""Santander credit-card model tests (M16-P2c)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.external.santander_models import SantanderCard, SantanderTransaction

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture() -> dict:
    return json.loads((_FIXTURES / "santander_cc_transactions.json").read_text())


def test_card_parses_aliased_fields():
    raw = _fixture()["cards_response"]["cards"][0]
    card = SantanderCard.model_validate(raw)
    assert card.card_id == "TEST-CARD-1"
    assert card.last4 == "1111"
    assert card.product_name == "1plus Visa Card"


def test_transaction_parses_eur_and_fx():
    txs = _fixture()["transactions_response"]["transactions"]
    eur = SantanderTransaction.model_validate(txs[0])
    assert eur.transaction_id == "TEST-TX-EUR"
    assert eur.booking_date == date(2026, 5, 4)
    assert eur.amount == Decimal("-42.50")
    assert eur.currency == "EUR"
    assert eur.is_fx is False

    usd = SantanderTransaction.model_validate(txs[1])
    assert usd.original_amount == Decimal("-99.00")
    assert usd.original_currency == "USD"
    assert usd.is_fx is True


def test_transaction_without_id_is_allowed():
    """Santander gives no stable id for some bookings — the model permits it."""
    pending = _fixture()["transactions_response"]["transactions"][3]
    tx = SantanderTransaction.model_validate(pending)
    assert tx.transaction_id is None
    assert tx.pending is True


def test_missing_required_field_raises():
    """A drifted payload without booking_date fails loudly, not silently."""
    with pytest.raises(ValidationError):
        SantanderTransaction.model_validate({"merchant": "X", "amount": "-1.00"})
