"""Canonical mapping for PayPal transactions (M16-P2b).

The PayPal connector's adapter into the *same* canonical dict shape that
:func:`src.normalization.canonicalize.canonicalize` produces for
Comdirect — so ingest, hashing, the pipeline and every downstream
consumer stay source-agnostic. The only differences are intrinsic to the
source:

* ``source`` is ``"paypal"`` — selects the hashing rule in
  :func:`src.normalization.canonicalize.content_hash`;
* ``sender_iban`` / ``recipient_iban`` are always ``None`` — PayPal
  carries no IBANs. The counterparty lands in ``sender`` / ``recipient``
  as the merchant / payer name (or e-mail fallback);
* ``currency`` is passed through verbatim — a USD or GBP PayPal payment
  stays in its own currency (no blind EUR cast); the FX-aware downstream
  handles conversion.

``external_id`` is PayPal's ``transaction_id`` — stable and unique, so
``(source, external_id)`` scoping makes a re-fetched window idempotent.
"""

from __future__ import annotations

import re
from typing import Any

from src.external.paypal_models import PayPalTransaction
from src.normalization.canonicalize import _parse_date, _to_decimal

# PayPal transaction_event_code → human label, for the rare transactions
# that carry neither a subject/note nor cart items. Kept deliberately
# small: only codes that need a *recognisable* description downstream.
# "Bank Deposit to PP Account" is the phrase the cross-source
# internal-transfer matcher keys on — see
# `NormalizationPipeline._flag_cross_source_transfers`.
_EVENT_CODE_LABELS: dict[str, str] = {
    "T0300": "Bank Deposit to PP Account",  # general add-funds from a bank
    "T0700": "Withdrawal to Bank Account",
}

# Funding codes whose canonical description must be the stable
# "Bank Deposit to PP Account" phrase regardless of any subject text.
_BANK_FUNDING_EVENT_CODES = frozenset({"T0300"})

# "PAYPAL *STEAMGAMES" / "PP*STEAM" → "STEAMGAMES" — strip the processor
# prefix so the bare vendor reaches the rule haystack / categoriser.
_PAYPAL_PREFIX_RE = re.compile(r"^\s*(?:paypal|pp)\s*\*\s*", re.IGNORECASE)


def strip_paypal_prefix(text: str | None) -> str | None:
    """Drop a leading ``PAYPAL *`` / ``PP*`` processor prefix from *text*."""
    if not text:
        return text
    return _PAYPAL_PREFIX_RE.sub("", text).strip() or None


def paypal_canonicalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one raw PayPal ``transaction_details`` dict onto the
    ten canonical fields shared with the Comdirect path."""
    tx = PayPalTransaction.model_validate(raw)
    info = tx.transaction_info

    amount = _to_decimal(info.transaction_amount.value)
    currency = info.transaction_amount.currency_code or "EUR"

    booking_date = _parse_date(info.transaction_initiation_date)

    counterparty = _counterparty_name(tx)
    description = _description(tx)

    # debit (amount < 0): money leaves the PayPal balance → counterparty
    # is the recipient. credit (>= 0): money arrives → counterparty is the
    # sender. IBANs are always None — PayPal has none.
    if amount < 0:
        sender, recipient = None, counterparty
    else:
        sender, recipient = counterparty, None

    return {
        "external_id": info.transaction_id,
        "booking_date": booking_date,
        "valuation_date": booking_date,
        "amount": amount,
        "currency": currency,
        "sender": sender,
        "recipient": recipient,
        "sender_iban": None,
        "recipient_iban": None,
        "description": description[:500] if description else None,
    }


def _counterparty_name(tx: PayPalTransaction) -> str | None:
    """Resolve the merchant / payer display name.

    Preference: business name (``alternate_full_name``) → personal name →
    first cart item name → payer e-mail. Every candidate is run through
    :func:`strip_paypal_prefix` so ``PAYPAL *VENDOR`` collapses to the
    bare vendor.
    """
    payer = tx.payer_info
    if payer and payer.payer_name:
        name = payer.payer_name
        if name.alternate_full_name:
            return strip_paypal_prefix(name.alternate_full_name)
        full = " ".join(p for p in (name.given_name, name.surname) if p).strip()
        if full:
            return strip_paypal_prefix(full)

    items = tx.cart_info.item_details if tx.cart_info else []
    for item in items:
        if item.item_name:
            return strip_paypal_prefix(item.item_name)

    if payer and payer.email_address:
        return payer.email_address
    return None


def _description(tx: PayPalTransaction) -> str | None:
    """Build the canonical free-text description.

    Bank-funding transactions are forced to the stable
    "Bank Deposit to PP Account" phrase so the cross-source
    internal-transfer matcher can recognise them deterministically.
    """
    info = tx.transaction_info
    code = info.transaction_event_code
    if code in _BANK_FUNDING_EVENT_CODES:
        return _EVENT_CODE_LABELS[code]

    if info.transaction_subject:
        return info.transaction_subject.strip()
    if info.transaction_note:
        return info.transaction_note.strip()

    items = tx.cart_info.item_details if tx.cart_info else []
    item_names = [
        stripped
        for item in items
        if (stripped := strip_paypal_prefix(item.item_name))
    ]
    if item_names:
        return ", ".join(item_names)

    if code and code in _EVENT_CODE_LABELS:
        return _EVENT_CODE_LABELS[code]
    return None
