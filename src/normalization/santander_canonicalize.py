"""Canonical mapping for Santander credit-card transactions (M16-P2c).

The Santander connector's adapter into the *same* canonical dict shape that
:func:`src.normalization.canonicalize.canonicalize` produces for Comdirect and
:func:`src.normalization.paypal_canonicalize.paypal_canonicalize` for PayPal —
so ingest, hashing, the pipeline and every downstream consumer stay
source-agnostic. Source-intrinsic differences:

* ``source`` is ``"santander_cc"`` — selects the hashing rule in
  :func:`src.normalization.canonicalize.content_hash`;
* ``sender_iban`` / ``recipient_iban`` are always ``None`` — a credit card
  carries no IBAN. The merchant lands in ``recipient`` (debit / purchase) or
  ``sender`` (credit / refund), mirroring the PayPal adapter;
* ``currency`` is the EUR settlement currency; ``amount`` the EUR settlement
  amount. **FX is preserved**: ``original_amount`` + ``original_currency``
  ride along as two *extra* canonical keys (outside the ten hashed identity
  fields) so a foreign-currency purchase keeps its original leg — no blind
  EUR cast. The pipeline persists them to ``normalized_transactions``.

``external_id`` is Santander's transaction id when present. Santander does
**not** guarantee a stable per-booking id, so when it is absent the adapter
derives a deterministic id — a hash over ``(booking_date, amount, merchant,
card_last4)`` — so a re-fetched window stays idempotent under the
``(source, external_id)`` scoping.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.external.santander_models import SantanderTransaction
from src.normalization.canonicalize import _to_decimal

#: Prefix on a derived id so a synthetic id is recognisable in the DB and
#: never collides with a real Santander transaction id.
_DERIVED_ID_PREFIX = "santander-"


def derive_external_id(tx: SantanderTransaction) -> str:
    """Deterministic id for a transaction Santander gave no stable id.

    SHA256 over ``(booking_date, amount, merchant, card_last4)`` — the fields
    that identify a card purchase. Two raw payloads describing the same
    purchase derive the same id and dedupe; two genuinely distinct purchases
    differ in at least one field.
    """
    parts = "|".join(
        [
            tx.booking_date.isoformat(),
            str(_to_decimal(tx.amount)),
            (tx.merchant_name or "").strip().lower(),
            (tx.card_last4 or "").strip(),
        ]
    )
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]
    return f"{_DERIVED_ID_PREFIX}{digest}"


def santander_canonicalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one raw Santander credit-card transaction onto the canonical dict.

    Returns the ten shared identity fields plus the two FX extras
    (``original_amount`` / ``original_currency``).
    """
    tx = SantanderTransaction.model_validate(raw)

    amount = _to_decimal(tx.amount)
    currency = (tx.currency or "EUR").upper()
    merchant = (tx.merchant_name or "").strip() or None

    external_id = tx.transaction_id or derive_external_id(tx)
    booking_date = tx.booking_date
    valuation_date = tx.valuation_date or booking_date

    # debit (amount < 0 — a purchase): money leaves the card → merchant is
    # the recipient. credit (>= 0 — a refund / credit): merchant is the
    # sender. IBANs are always None — a credit card has none.
    if amount < 0:
        sender, recipient = None, merchant
    else:
        sender, recipient = merchant, None

    original_amount = (
        _to_decimal(tx.original_amount) if tx.original_amount is not None else None
    )
    original_currency = (
        tx.original_currency.upper()
        if tx.original_currency and tx.is_fx
        else None
    )

    return {
        "external_id": external_id,
        "booking_date": booking_date,
        "valuation_date": valuation_date,
        "amount": amount,
        "currency": currency,
        "sender": sender,
        "recipient": recipient,
        "sender_iban": None,
        "recipient_iban": None,
        "description": merchant,
        # --- FX extras — outside CANONICAL_FIELDS_FOR_HASH, persisted by the
        #     pipeline to normalized_transactions.original_amount / _currency.
        "original_amount": original_amount if original_currency else None,
        "original_currency": original_currency,
    }
