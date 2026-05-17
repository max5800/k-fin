"""Tests for the Santander 1plus-Card statement PDF parser.

Drives the parsing logic over inline dummy statement *text* (the layout
``pdfplumber`` extracts), so every branch — date / year inference, the ``H``
credit flag, foreign-currency rows, fee rows, a credit (``H``) balance, the
opening/closing balance reconciliation and collision-free ids — is covered
without a binary PDF fixture. Dummy merchants and amounts only.
"""

from __future__ import annotations

import pytest

from src.normalization.canonicalize import CANONICAL_FIELDS_FOR_HASH
from src.normalization.santander_canonicalize import santander_canonicalize
from src.normalization.santander_pdf import (
    SantanderPdfError,
    _parse_statement_text,
    parse_statement,
)


def _statement(
    *,
    date: str = "04.06.2026",
    card: str = "0000",
    opening: str = "100,00",
    lines: tuple[str, ...] = (),
    closing: str = "100,00",
) -> str:
    """Build dummy statement text in the layout ``pdfplumber`` extracts."""
    head = f"Rechnungsdatum {date}\n1plus Visa-Card\n************{card}\n"
    body = f"SALDOVORTRAG {opening}\n"
    if lines:
        body += "\n".join(lines) + "\n"
    body += f"NEUER SALDO IN EURO {closing}\n"
    return head + body


def test_basic_purchase():
    # opening 100,00; one 40,00 debit; closing = 100 - (-40) = 140,00
    txns = _parse_statement_text(
        _statement(lines=("1505 Test Merchant 1205 40,00",), closing="140,00")
    )
    assert len(txns) == 1
    txn = txns[0]
    assert txn["amount"] == "-40.00"  # a purchase is negative
    assert txn["booking_date"] == "2026-05-15"
    assert txn["valuation_date"] == "2026-05-12"
    assert txn["merchant_name"] == "Test Merchant"
    assert txn["currency"] == "EUR"
    assert txn["card_last4"] == "0000"
    assert txn["original_currency"] is None


def test_credit_row_marked_h_is_positive():
    # opening 100,00; one 30,00 credit; closing = 100 - 30 = 70,00
    txns = _parse_statement_text(
        _statement(lines=("1705 Zahlung Lastschrift 1705 30,00 H",), closing="70,00")
    )
    assert txns[0]["amount"] == "30.00"


def test_fx_row_keeps_original_amount_and_currency():
    # opening 100,00; foreign-currency debit settling to 45,00 EUR; closing 145,00
    txns = _parse_statement_text(
        _statement(
            lines=("2005 Foreign Shop USD 50,00 0.9000 1805 45,00",),
            closing="145,00",
        )
    )
    txn = txns[0]
    assert txn["amount"] == "-45.00"
    assert txn["original_amount"] == "-50.00"
    assert txn["original_currency"] == "USD"
    assert txn["merchant_name"] == "Foreign Shop"


def test_fee_rows_become_transactions_at_statement_date():
    # opening 100,00; two fee charges; closing = 100 - (-4 - 1) = 105,00
    txns = _parse_statement_text(
        _statement(
            lines=("FINANZIERUNGSGEBÜHR 4,00", "VERZUGSZINSEN 1,00"),
            closing="105,00",
        )
    )
    assert {t["merchant_name"] for t in txns} == {"Finanzierungsgebühr", "Verzugszinsen"}
    assert all(t["amount"].startswith("-") for t in txns)  # fees are charges
    assert all(t["booking_date"] == "2026-06-04" for t in txns)  # statement date


def test_credit_balance_h_on_balance_rows_flips_sign():
    # SALDOVORTRAG carries "H" — a credit balance, i.e. -50,00.
    # one 40,00 debit → closing = -50 - (-40) = -10 → "10,00 H"
    txns = _parse_statement_text(
        _statement(
            opening="50,00 H",
            lines=("1505 Shop 1505 40,00",),
            closing="10,00 H",
        )
    )
    assert len(txns) == 1
    assert txns[0]["amount"] == "-40.00"


def test_december_transaction_on_january_statement_wraps_year():
    # January statement, a December booking → previous year
    txns = _parse_statement_text(
        _statement(
            date="06.01.2026",
            lines=("2912 Shop 2812 40,00",),
            closing="140,00",
        )
    )
    assert txns[0]["booking_date"] == "2025-12-29"
    assert txns[0]["valuation_date"] == "2025-12-28"


def test_balance_mismatch_raises():
    with pytest.raises(SantanderPdfError, match="balance check"):
        _parse_statement_text(
            _statement(lines=("1505 Shop 1205 40,00",), closing="999,99")
        )


def test_identical_rows_get_distinct_ids():
    # two fully identical rows — the per-statement occurrence index keeps the
    # derived ids distinct; closing = 100 - (-80) = 180,00
    txns = _parse_statement_text(
        _statement(
            lines=("1505 Shop 1205 40,00", "1505 Shop 1205 40,00"),
            closing="180,00",
        )
    )
    assert len(txns) == 2
    assert txns[0]["transaction_id"] != txns[1]["transaction_id"]


def test_reparsing_yields_stable_ids():
    text = _statement(lines=("1505 Shop 1205 40,00",), closing="140,00")
    assert (
        _parse_statement_text(text)[0]["transaction_id"]
        == _parse_statement_text(text)[0]["transaction_id"]
    )


def test_thousands_separator_amount():
    # opening 100,00; debit 1.234,56; closing = 100 - (-1234.56) = 1.334,56
    txns = _parse_statement_text(
        _statement(lines=("1505 Big Shop 1205 1.234,56",), closing="1.334,56")
    )
    assert txns[0]["amount"] == "-1234.56"


def test_text_without_rechnungsdatum_raises():
    with pytest.raises(SantanderPdfError, match="Rechnungsdatum"):
        _parse_statement_text("just some random text\nno statement here")


def test_parse_statement_rejects_non_pdf_bytes():
    with pytest.raises(SantanderPdfError, match="could not read PDF"):
        parse_statement(b"this is not a pdf at all")


def test_parsed_row_feeds_santander_canonicalize():
    """A parsed row must round-trip through the M16-P2c canonical adapter."""
    txns = _parse_statement_text(
        _statement(lines=("1505 Test Merchant 1205 40,00",), closing="140,00")
    )
    canonical = santander_canonicalize(txns[0])
    assert set(CANONICAL_FIELDS_FOR_HASH) <= set(canonical)
    # a debit → the merchant is the recipient (money leaves the card)
    assert canonical["recipient"] == "Test Merchant"
    assert canonical["sender"] is None
