"""Shared canonical mapping from a raw Comdirect payload dict to the
flat, normalized shape used everywhere downstream (hash, ingest, pipeline).

The pipeline and the ingest bridge must agree on *exactly* which fields
define a transaction's identity — otherwise a re-ingest produces a
different hash for the same data, and idempotency breaks. This module
is the single definition.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

CANONICAL_FIELDS_FOR_HASH = (
    "comdirect_id",
    "booking_date",
    "valuation_date",
    "amount",
    "currency",
    "sender",
    "recipient",
    "sender_iban",
    "recipient_iban",
    "description",
)


def canonicalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw JSON-export transaction dict onto canonical fields.

    Accepts both the nested Comdirect API shape (e.g. `transactionValue`,
    `creditor`, `bookingDate`) and the flat shape emitted by
    `src.connector.models.ComdirectTransaction` (e.g. `amount`,
    `creditor_name`, `booking_date`).
    """
    tx_value = raw.get("transactionValue") or raw.get("amount_raw") or {}
    creditor = raw.get("creditor") or {}
    debtor = raw.get("debtor") or {}

    if isinstance(tx_value, dict):
        amount_src = tx_value.get("value")
        currency = tx_value.get("unit") or raw.get("currency") or "EUR"
    else:
        amount_src = raw.get("amount") if raw.get("amount") is not None else tx_value
        currency = raw.get("currency") or "EUR"

    amount = _to_decimal(amount_src if amount_src is not None else raw.get("amount", 0))

    booking_date = raw.get("bookingDate") or raw.get("booking_date") or ""
    valuation_date = (
        raw.get("valutaDate")
        or raw.get("value_date")
        or raw.get("valuation_date")
        or booking_date
    )

    # Nested API shape has creditor/debtor dicts with holderName/iban;
    # flat model_dump() shape has creditor_name/creditor_iban directly.
    # An empty dict is truthy, so we must check for actual content.
    creditor_name = (
        creditor.get("holderName")
        if creditor.get("holderName")
        else raw.get("creditor_name", "")
    )
    creditor_iban = (
        creditor.get("iban") if creditor.get("iban") else raw.get("creditor_iban", "")
    )
    debtor_name = (
        debtor.get("holderName")
        if debtor.get("holderName")
        else raw.get("debtor_name", "")
    )
    debtor_iban = (
        debtor.get("iban") if debtor.get("iban") else raw.get("debtor_iban", "")
    )

    # debit (negative amount): money flows to creditor → recipient=creditor
    # credit (positive amount): money comes from debtor → sender=debtor
    if amount < 0:
        sender = None
        sender_iban = None
        recipient = creditor_name or None
        recipient_iban = creditor_iban or None
    else:
        sender = debtor_name or None
        sender_iban = debtor_iban or None
        recipient = None
        recipient_iban = None

    description = _clean_remittance_info(
        raw.get("remittanceInfo")
        or raw.get("remittance_info")
        or raw.get("description")
        or raw.get("typeText")
        or raw.get("type_text")
        or ""
    )

    return {
        "comdirect_id": raw.get("transactionId") or raw.get("transaction_id") or None,
        "booking_date": _parse_date(booking_date),
        "valuation_date": _parse_date(valuation_date),
        "amount": amount,
        "currency": currency,
        "sender": sender,
        "recipient": recipient,
        "sender_iban": sender_iban,
        "recipient_iban": recipient_iban,
        "description": description[:500] if description else None,
    }


def content_hash(canonical: dict[str, Any]) -> str:
    """SHA256 over the canonical identity fields.

    Two raw payloads that project to identical canonical values will
    share a hash; Comdirect corrections that change any identity field
    produce a new hash.
    """
    payload = {k: _json_default(canonical.get(k)) for k in CANONICAL_FIELDS_FOR_HASH}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clean_remittance_info(raw_text: str) -> str:
    """Strip SWIFT/MT940 numbered field prefixes from remittance info.

    Comdirect sometimes returns structured remittance data with numbered
    prefixes like ``01Aral Station...02Karte Nr...``.  This strips the
    ``\\d{2}`` prefixes and joins the fragments with ``", "``.
    """
    if not raw_text:
        return ""
    # Detect numbered prefix pattern: two digits at start or preceded by
    # whitespace/boundary, followed by non-digit content.
    if re.match(r"^\d{2}\D", raw_text):
        parts = re.split(r"(?:^|\s)(\d{2})(?=\D)", raw_text)
        # split produces: ['', '01', 'content', '02', 'content', ...]
        cleaned = [
            p.strip() for p in parts if p.strip() and not re.fullmatch(r"\d{2}", p)
        ]
        return ", ".join(cleaned) if cleaned else raw_text.strip()
    return raw_text.strip()


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
