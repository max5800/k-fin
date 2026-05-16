"""Canonicalize roundtrip tests for the PayPal adapter (M16-P2b).

Drives `paypal_canonicalize` over the dummy sandbox fixtures and checks
the canonical dict shape matches the Comdirect path: same ten keys, the
same `content_hash` discipline, IBANs always None, FX currencies kept.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.normalization.canonicalize import CANONICAL_FIELDS_FOR_HASH, content_hash
from src.normalization.paypal_canonicalize import paypal_canonicalize, strip_paypal_prefix

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_transactions() -> list[dict]:
    data = json.loads(
        (_FIXTURES / "paypal_transactions.json").read_text(encoding="utf-8")
    )
    return data["transaction_details"]


def _by_id() -> dict[str, dict]:
    return {
        c["external_id"]: c
        for c in (paypal_canonicalize(raw) for raw in _fixture_transactions())
    }


def test_canonical_has_exactly_the_shared_fields():
    """Every PayPal canonical dict carries the same keys the hash consumes."""
    for raw in _fixture_transactions():
        canonical = paypal_canonicalize(raw)
        assert set(canonical) == set(CANONICAL_FIELDS_FOR_HASH)


def test_ibans_are_always_none():
    """PayPal carries no IBANs — both IBAN fields stay None."""
    for raw in _fixture_transactions():
        canonical = paypal_canonicalize(raw)
        assert canonical["sender_iban"] is None
        assert canonical["recipient_iban"] is None


def test_debit_purchase_maps_merchant_to_recipient():
    """A negative amount → recipient is the merchant, sender is None."""
    tx = _by_id()["TEST-TXN-PURCHASE-EUR"]
    assert tx["amount"] == Decimal("-19.99")
    assert tx["currency"] == "EUR"
    assert tx["sender"] is None
    # "PAYPAL *STEAMGAMES" → prefix stripped to the bare vendor.
    assert tx["recipient"] == "STEAMGAMES"
    assert tx["description"] == "Steam Wallet Code"


def test_credit_received_maps_payer_to_sender():
    """A positive amount → sender is the payer, recipient is None."""
    tx = _by_id()["TEST-TXN-RECEIVED-EUR"]
    assert tx["amount"] == Decimal("25.00")
    assert tx["sender"] == "Max Mustermann"
    assert tx["recipient"] is None
    assert tx["description"] == "Splitwise Ausgleich"


def test_bank_deposit_gets_stable_description():
    """The T0300 funding event canonicalizes to the phrase the
    cross-source transfer matcher keys on."""
    tx = _by_id()["TEST-TXN-BANK-DEPOSIT"]
    assert tx["amount"] == Decimal("50.00")
    assert tx["description"] == "Bank Deposit to PP Account"


def test_foreign_currency_is_preserved():
    """A USD payment keeps its currency — no blind EUR cast."""
    tx = _by_id()["TEST-TXN-PURCHASE-USD"]
    assert tx["currency"] == "USD"
    assert tx["amount"] == Decimal("-10.00")
    # subject/note absent → description falls back to the cart item name.
    assert tx["description"] == "E-Book: Async Python"


def test_content_hash_is_stable_and_source_scoped():
    """The same payload hashes identically twice; the paypal hash differs
    from the comdirect hash (different source rule)."""
    raw = _fixture_transactions()[0]
    canonical = paypal_canonicalize(raw)
    h1 = content_hash(canonical, source="paypal")
    h2 = content_hash(canonical, source="paypal")
    assert h1 == h2
    assert h1 != content_hash(canonical, source="comdirect")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PAYPAL *STEAMGAMES", "STEAMGAMES"),
        ("PP*NETFLIX.COM", "NETFLIX.COM"),
        ("Regular Merchant", "Regular Merchant"),
        (None, None),
        ("", ""),
    ],
)
def test_strip_paypal_prefix(raw, expected):
    assert strip_paypal_prefix(raw) == expected
