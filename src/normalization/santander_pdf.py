"""Parser for Santander 1plus Visa credit-card PDF statements (M16-P2c).

Santander Consumer Bank's PSD2 / XS2A interface exposes current accounts only —
the 1plus Visa credit card is not an XS2A-reachable payment account, so the card
cannot be synced through an account-information aggregator. The ingestion route
is therefore the official monthly statement ("Rechnung / Kontoauszug") the
cardholder downloads from MySantander as a PDF.

This module parses that PDF into the flat ``SantanderTransaction`` shape the
canonical adapter (:func:`src.normalization.santander_canonicalize.santander_canonicalize`)
consumes, so ingest, hashing and the normalization pipeline stay
source-agnostic — the PDF importer reuses the entire M16-P2c canonical/model
layer; only this parsing front-end is new.

Statement layout
----------------
The statement is a machine-generated, ruled transaction table framed by a
``SALDOVORTRAG`` (opening balance) and a ``NEUER SALDO IN EURO`` (closing
balance) row. ``pdfplumber``'s table extraction collapses each column into one
multi-line cell and the per-column line counts do not align (rows without a
location or booking date drop values), so parsing works off ``extract_text()``,
which linearises every row cleanly. A transaction line carries::

    <booking ddmm> <merchant + location> [<cur> <fx-amount> <rate>] <purchase ddmm> <eur-amount>[ H]

* dates are ``DDMM`` without a year — the year is inferred from the statement's
  ``Rechnungsdatum`` (a month greater than the statement month belongs to the
  previous year — the only wrap a monthly statement can produce);
* a trailing ``H`` ("Haben") marks a credit — a refund, a rebate, or the
  direct-debit settlement of the previous statement;
* foreign-currency purchases carry the original amount + currency (the 1plus
  card waives the conversion fee, so that column stays empty);
* fee / interest rows (``FINANZIERUNGSGEBÜHR``, ``VERZUGSZINSEN``, …) carry no
  date — surfaced as a transaction dated at the statement date;
* the opening / closing balance may itself be a credit (trailing ``H``).

Sign convention: a purchase/fee is negative, a credit positive — matching the
canonical adapter and every other source.

Defensive integrity check
-------------------------
Every parsed statement is balance-checked: ``opening - sum(amounts) == closing``
must hold exactly (a credit-card balance rises with spending, and the canonical
sign is spending-negative). A mismatch means a row was missed or misread and
raises :class:`SantanderPdfError` rather than ingesting a partial statement.
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from decimal import Decimal
from typing import Any, BinaryIO

import pdfplumber

__all__ = ["SantanderPdfError", "parse_statement"]


class SantanderPdfError(ValueError):
    """A PDF did not match the expected Santander 1plus-Card statement layout."""


# German-formatted money: optional thousands dots, comma decimal — `1.234,56`.
_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"

_STATEMENT_DATE_RE = re.compile(r"Rechnungsdatum\s+(\d{2})\.(\d{2})\.(\d{4})")
_CARD_LAST4_RE = re.compile(r"\*{4,}\s*(\d{4})")
# Opening / closing balance rows — a trailing `H` ("Haben") marks a credit
# balance (the cardholder is in credit, not owing) and flips the sign.
_OPENING_RE = re.compile(r"^SALDOVORTRAG\s+(" + _AMOUNT + r")(\s+H)?\s*$")
_CLOSING_RE = re.compile(r"^NEUER SALDO IN EURO\s+(" + _AMOUNT + r")(\s+H)?\s*$")
# Fee / interest rows — no date columns, charged at the statement date. An
# explicit allowlist: an unknown fee label fails the balance check loudly
# rather than being silently dropped.
_FEE_RE = re.compile(
    r"^(?P<label>FINANZIERUNGSGEBÜHR|VERZUGSZINSEN|SOLLZINSEN|MAHNGEBÜHR)\s+"
    r"(?P<amount>" + _AMOUNT + r")(?P<credit>\s+H)?\s*$"
)
# A transaction row: booking DDMM, free-text middle, purchase DDMM, EUR amount.
_TXN_RE = re.compile(
    r"^(?P<booking>\d{4})\s+(?P<mid>.+?)\s+(?P<purchase>\d{4})\s+"
    r"(?P<amount>" + _AMOUNT + r")(?P<credit>\s+H)?$"
)
# Foreign-currency tail inside the middle: `<merchant> USD 29,75 0.8504`.
_FX_RE = re.compile(
    r"^(?P<merchant>.+?)\s+(?P<currency>[A-Z]{3})\s+"
    r"(?P<fx_amount>" + _AMOUNT + r")\s+(?P<rate>\d+\.\d{3,})$"
)


def _to_decimal(value: str) -> Decimal:
    """Parse a German-formatted amount (``1.234,56``) into a :class:`Decimal`."""
    return Decimal(value.replace(".", "").replace(",", "."))


def _resolve_date(ddmm: str, stmt_year: int, stmt_month: int) -> date:
    """Resolve a ``DDMM`` field to a full date against the statement month.

    A month greater than the statement's own month belongs to the previous
    year — the December/January wrap a monthly statement can produce.
    """
    day, month = int(ddmm[:2]), int(ddmm[2:])
    year = stmt_year - 1 if month > stmt_month else stmt_year
    return date(year, month, day)


def _build(
    *,
    booking: date,
    purchase: date,
    merchant: str,
    amount: Decimal,
    original_amount: Decimal | None,
    original_currency: str | None,
    card_last4: str | None,
    statement_date: date,
    seen_counts: dict[tuple[str, ...], int],
) -> dict[str, Any]:
    """Assemble one ``SantanderTransaction``-shaped dict with a deterministic id.

    Santander gives the PDF no per-transaction id, so the id is a hash over the
    distinguishing fields plus a per-statement occurrence index — two genuinely
    identical rows on one statement (same date, merchant, amount) still get
    distinct, re-import-stable ids.
    """
    merchant = merchant.strip()
    key = (
        statement_date.isoformat(),
        booking.isoformat(),
        purchase.isoformat(),
        str(amount),
        merchant.lower(),
        original_currency or "",
        card_last4 or "",
    )
    index = seen_counts.get(key, 0)
    seen_counts[key] = index + 1
    digest = hashlib.sha256(
        ("|".join(key) + f"|{index}").encode("utf-8")
    ).hexdigest()[:16]
    return {
        "transaction_id": f"santander-pdf-{digest}",
        "booking_date": booking.isoformat(),
        "valuation_date": purchase.isoformat(),
        "merchant_name": merchant or None,
        "merchant_country": None,
        "amount": str(amount),
        "currency": "EUR",
        "original_amount": str(original_amount) if original_amount is not None else None,
        "original_currency": original_currency,
        "card_last4": card_last4,
        "pending": False,
    }


def parse_statement(source: str | bytes | BinaryIO) -> list[dict[str, Any]]:
    """Parse one Santander 1plus-Card statement PDF into transaction dicts.

    ``source`` is a file path, the raw PDF bytes, or an open binary stream.
    Returns a list of dicts in
    :class:`~src.external.santander_models.SantanderTransaction` shape — ready
    for :func:`~src.normalization.santander_canonicalize.santander_canonicalize`.

    Raises :class:`SantanderPdfError` when the PDF is not a recognisable
    statement or fails the opening/closing balance reconciliation.
    """
    pdf_source: Any = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        with pdfplumber.open(pdf_source) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # pdfplumber / pdfminer surface a broad range
        raise SantanderPdfError(f"could not read PDF: {exc}") from exc

    return _parse_statement_text(text)


def _parse_statement_text(text: str) -> list[dict[str, Any]]:
    """Parse the extracted statement text into transaction dicts.

    Split out from :func:`parse_statement` so the line-by-line parsing and
    the opening/closing balance reconciliation can be unit-tested on a text
    fixture, without a binary PDF.
    """
    date_match = _STATEMENT_DATE_RE.search(text)
    if date_match is None:
        raise SantanderPdfError(
            "no 'Rechnungsdatum' found — not a Santander 1plus-Card statement?"
        )
    stmt_day, stmt_month, stmt_year = (int(g) for g in date_match.groups())
    statement_date = date(stmt_year, stmt_month, stmt_day)

    card_match = _CARD_LAST4_RE.search(text)
    card_last4 = card_match.group(1) if card_match else None

    opening: Decimal | None = None
    closing: Decimal | None = None
    transactions: list[dict[str, Any]] = []
    seen_counts: dict[tuple[str, ...], int] = {}
    in_table = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        opening_match = _OPENING_RE.match(line)
        if opening_match is not None:
            opening = _to_decimal(opening_match.group(1))
            if opening_match.group(2):  # trailing "H" — credit balance
                opening = -opening
            in_table = True
            continue
        if not in_table:
            continue

        closing_match = _CLOSING_RE.match(line)
        if closing_match is not None:
            closing = _to_decimal(closing_match.group(1))
            if closing_match.group(2):  # trailing "H" — credit balance
                closing = -closing
            break

        fee_match = _FEE_RE.match(line)
        if fee_match is not None:
            fee_eur = _to_decimal(fee_match.group("amount"))
            transactions.append(
                _build(
                    booking=statement_date,
                    purchase=statement_date,
                    merchant=fee_match.group("label").title(),
                    amount=fee_eur if fee_match.group("credit") else -fee_eur,
                    original_amount=None,
                    original_currency=None,
                    card_last4=card_last4,
                    statement_date=statement_date,
                    seen_counts=seen_counts,
                )
            )
            continue

        txn_match = _TXN_RE.match(line)
        if txn_match is None:
            continue  # repeated headers / page furniture / footer text

        try:
            is_credit = txn_match.group("credit") is not None
            eur = _to_decimal(txn_match.group("amount"))
            amount = eur if is_credit else -eur
            booking = _resolve_date(txn_match.group("booking"), stmt_year, stmt_month)
            purchase = _resolve_date(txn_match.group("purchase"), stmt_year, stmt_month)
        except (ValueError, ArithmeticError):
            # A line that matched the shape but carries a non-date / non-amount
            # is not a real transaction — skip; the balance check is the net.
            continue

        mid = txn_match.group("mid").strip()
        original_amount: Decimal | None = None
        original_currency: str | None = None
        fx_match = _FX_RE.match(mid)
        if fx_match is not None:
            merchant = fx_match.group("merchant").strip()
            original_currency = fx_match.group("currency")
            fx_value = _to_decimal(fx_match.group("fx_amount"))
            original_amount = fx_value if is_credit else -fx_value
        else:
            merchant = mid

        transactions.append(
            _build(
                booking=booking,
                purchase=purchase,
                merchant=merchant,
                amount=amount,
                original_amount=original_amount,
                original_currency=original_currency,
                card_last4=card_last4,
                statement_date=statement_date,
                seen_counts=seen_counts,
            )
        )

    if opening is None or closing is None:
        raise SantanderPdfError(
            "statement table markers (SALDOVORTRAG / NEUER SALDO) not found"
        )

    booked = sum((Decimal(t["amount"]) for t in transactions), Decimal("0"))
    if opening - booked != closing:
        raise SantanderPdfError(
            f"balance check failed: opening - booked != closing "
            f"(delta {opening - booked - closing}) — a transaction row was "
            f"missed or misread"
        )

    return transactions
