"""Tests for the PayPal Kontoauszug CSV importer (parser + canonical adapter).

Drives :func:`parse_paypal_csv` and :func:`paypal_csv_canonicalize` over
inline dummy German-locale CSVs — header mapping, German number / date
formats, the loud failure on a missing required column, the
real-vs-funding row classification, and the canonical dict shape shared
with the Comdirect / Santander paths.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from src.normalization.canonicalize import CANONICAL_FIELDS_FOR_HASH, content_hash
from src.normalization.paypal_csv import (
    PayPalCsvParseError,
    parse_paypal_csv,
    paypal_csv_canonicalize,
    strip_paypal_prefix,
)

# The PayPal "Kontoauszug" column set (real export header).
_HEADERS = [
    "Datum", "Uhrzeit", "Zeitzone", "Beschreibung", "Währung", "Brutto",
    "Entgelt", "Netto", "Guthaben", "Transaktionscode",
    "Absender E-Mail-Adresse", "Name", "Name der Bank", "Bankkonto",
    "Versand- und Bearbeitungsgebühr", "Umsatzsteuer", "Rechnungsnummer",
    "Zugehöriger Transaktionscode",
]


def _cell(**kw: str) -> dict[str, str]:
    """One Kontoauszug row — a real merchant payment by default; kwargs
    override individual German-header cells."""
    base = {h: "" for h in _HEADERS}
    base.update(
        {
            "Datum": "08.05.2026",
            "Uhrzeit": "14:23:01",
            "Zeitzone": "Europe/Berlin",
            "Beschreibung": "Zahlung im Einzugsverfahren mit Zahlungsrechnung",
            "Währung": "EUR",
            "Brutto": "-19,99",
            "Entgelt": "0,00",
            "Netto": "-19,99",
            "Guthaben": "-19,99",
            "Transaktionscode": "TEST-TXN-0001",
            "Name": "Test Merchant Ltd",
        }
    )
    base.update(kw)
    return base


def _csv(rows: list[dict], headers: list[str] = _HEADERS, bom: bool = False) -> bytes:
    """Serialise dummy rows into PayPal-style Kontoauszug CSV bytes."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buf.getvalue().encode("utf-8-sig" if bom else "utf-8")


# ── parser ──────────────────────────────────────────────────────────────


def test_parse_basic_row():
    rows = parse_paypal_csv(_csv([_cell()]))
    assert len(rows) == 1
    row = rows[0]
    assert row["transaction_id"] == "TEST-TXN-0001"
    assert row["date"] == "2026-05-08"  # DD.MM.YYYY → ISO
    assert row["gross"] == "-19.99"  # German decimal → dot-decimal string
    assert row["currency"] == "EUR"
    assert row["name"] == "Test Merchant Ltd"


def test_parse_handles_utf8_bom():
    """PayPal exports the file UTF-8-with-BOM — it must parse transparently."""
    rows = parse_paypal_csv(_csv([_cell()], bom=True))
    assert len(rows) == 1
    assert rows[0]["transaction_id"] == "TEST-TXN-0001"


def test_german_thousands_separator():
    rows = parse_paypal_csv(_csv([_cell(Brutto="-1.270,99")]))
    assert rows[0]["gross"] == "-1270.99"


def test_funding_rows_are_skipped():
    """A funding row (empty Name) is internal plumbing — skipped, the
    real merchant payment is kept."""
    rows = parse_paypal_csv(
        _csv(
            [
                _cell(Transaktionscode="REAL", Name="Test Merchant Ltd"),
                _cell(
                    Transaktionscode="FUNDING",
                    Name="",
                    Beschreibung="Bankgutschrift auf PayPal-Konto",
                    Brutto="19,99",
                ),
            ]
        )
    )
    assert [r["transaction_id"] for r in rows] == ["REAL"]


def test_paypal_internal_rows_are_skipped():
    """A row whose counterparty is PayPal itself (a buyer-credit
    settlement) is plumbing — skipped."""
    rows = parse_paypal_csv(
        _csv(
            [
                _cell(Transaktionscode="REAL", Name="Test Merchant Ltd"),
                _cell(
                    Transaktionscode="INTERNAL",
                    Name="PayPal (Europe) S.a.r.l. et Cie, SCA",
                    Beschreibung="Allgemeine Zahlung",
                ),
            ]
        )
    )
    assert [r["transaction_id"] for r in rows] == ["REAL"]


def test_missing_required_column_raises_naming_it():
    headers = [h for h in _HEADERS if h != "Transaktionscode"]
    with pytest.raises(PayPalCsvParseError, match="Transaktionscode"):
        parse_paypal_csv(_csv([_cell()], headers=headers))


def test_missing_name_column_raises():
    """Without ``Name`` the importer cannot tell a real payment from a
    funding row — it must fail loudly, not import everything."""
    headers = [h for h in _HEADERS if h != "Name"]
    with pytest.raises(PayPalCsvParseError, match="Name"):
        parse_paypal_csv(_csv([_cell()], headers=headers))


def test_unparseable_amount_raises_with_row_number():
    with pytest.raises(PayPalCsvParseError, match="row 2"):
        parse_paypal_csv(_csv([_cell(Brutto="not-a-number")]))


def test_unparseable_date_raises():
    with pytest.raises(PayPalCsvParseError, match="date"):
        parse_paypal_csv(_csv([_cell(Datum="2026-05-08")]))  # ISO, not DD.MM.YYYY


def test_blank_trailing_line_is_ignored():
    rows = parse_paypal_csv(_csv([_cell()]) + b"\r\n")
    assert len(rows) == 1


# ── canonicalize ────────────────────────────────────────────────────────


def _canon(raw_bytes: bytes) -> dict[str, dict]:
    """Parse then canonicalize → {external_id: canonical_dict}."""
    return {
        c["external_id"]: c
        for c in (paypal_csv_canonicalize(r) for r in parse_paypal_csv(raw_bytes))
    }


def test_canonical_carries_the_hash_fields_plus_fx_columns():
    """A PayPal canonical dict carries the ten shared hash fields plus
    the two FX columns — the same shape as the Santander adapter."""
    for canonical in _canon(_csv([_cell()])).values():
        assert set(CANONICAL_FIELDS_FOR_HASH) <= set(canonical)
        assert set(canonical) - set(CANONICAL_FIELDS_FOR_HASH) == {
            "original_amount",
            "original_currency",
        }


def test_debit_maps_merchant_to_recipient():
    """A negative Brutto → recipient is the merchant, sender is None."""
    tx = _canon(_csv([_cell()]))["TEST-TXN-0001"]
    assert tx["amount"] == Decimal("-19.99")
    assert tx["sender"] is None
    assert tx["recipient"] == "Test Merchant Ltd"
    assert tx["description"] == "Zahlung im Einzugsverfahren mit Zahlungsrechnung"
    assert tx["sender_iban"] is None and tx["recipient_iban"] is None


def test_credit_maps_payer_to_sender():
    """A positive Brutto → sender is the payer, recipient is None."""
    row = _cell(
        Transaktionscode="TEST-TXN-RECV",
        Name="John Doe",
        Beschreibung="Allgemeine Zahlung",
        Brutto="25,00",
        Netto="25,00",
    )
    tx = _canon(_csv([row]))["TEST-TXN-RECV"]
    assert tx["amount"] == Decimal("25.00")
    assert tx["sender"] == "John Doe"
    assert tx["recipient"] is None


def test_foreign_currency_without_conversion_leg_stays_verbatim():
    """A foreign-currency payment with no FX conversion leg in the export
    (e.g. paid from a foreign PayPal balance) stays in its own currency —
    a defensive fallback, FX columns left empty."""
    row = _cell(
        Transaktionscode="TEST-TXN-USD",
        Name="GitHub Inc",
        Währung="USD",
        Brutto="-10,00",
        Netto="-10,00",
    )
    tx = _canon(_csv([row]))["TEST-TXN-USD"]
    assert tx["currency"] == "USD"
    assert tx["amount"] == Decimal("-10.00")
    assert tx["original_amount"] is None
    assert tx["original_currency"] is None


def test_foreign_currency_converted_to_eur_via_conversion_leg():
    """A USD payment is booked in EUR via its 'Allgemeine
    Währungsumrechnung' leg; the USD original lands in the FX columns."""
    payment = _cell(
        Transaktionscode="TEST-TXN-USD",
        Name="GitHub Inc",
        Währung="USD",
        Brutto="-10,00",
        Netto="-10,00",
    )
    # The EUR conversion leg — empty Name (PayPal plumbing, not imported
    # on its own), linked to the payment via the related-transaction code.
    conversion = _cell(
        Transaktionscode="CONV-EUR-LEG",
        Name="",
        Beschreibung="Allgemeine Währungsumrechnung",
        Währung="EUR",
        Brutto="-9,20",
    )
    conversion["Zugehöriger Transaktionscode"] = "TEST-TXN-USD"

    canon = _canon(_csv([payment, conversion]))
    assert list(canon) == ["TEST-TXN-USD"]  # the conversion leg is not imported
    tx = canon["TEST-TXN-USD"]
    assert tx["currency"] == "EUR"
    assert tx["amount"] == Decimal("-9.20")
    assert tx["original_amount"] == Decimal("-10.00")
    assert tx["original_currency"] == "USD"


def test_content_hash_is_stable_and_source_scoped():
    tx = _canon(_csv([_cell()]))["TEST-TXN-0001"]
    h1 = content_hash(tx, source="paypal")
    h2 = content_hash(tx, source="paypal")
    assert h1 == h2
    assert h1 != content_hash(tx, source="comdirect")


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
