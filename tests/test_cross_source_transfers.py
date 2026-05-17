"""Cross-source internal-transfer matching tests (M16-P2b).

A PayPal "Bank Deposit to PP Account" credit and its Comdirect
"PAYPAL EUROPE" debit counterpart describe one money movement. The
pipeline must flag both `internal_transfer=True` so the savings-rate
maths counts the top-up once, not twice.
"""

from __future__ import annotations

import pandas as pd

from src.core.db.models import DataSource
from src.normalization.pipeline import NormalizationPipeline

_flag = NormalizationPipeline._flag_cross_source_transfers


def _row(**kw):
    base = {
        "id": kw["id"],
        "source": kw["source"],
        "amount": kw["amount"],
        "booking_date": kw["booking_date"],
        "description": kw.get("description"),
        "recipient": kw.get("recipient"),
        "sender_iban": kw.get("sender_iban"),
        "recipient_iban": kw.get("recipient_iban"),
        "internal_transfer": False,
    }
    return base


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_matching_pair_flags_both_sides():
    df = _frame(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-08",
                description="Bank Deposit to PP Account",
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-50.00,
                booking_date="2026-05-09",
                recipient="PAYPAL EUROPE SARL ET CIE",
            ),
        ]
    )
    out = _flag(df)
    flags = dict(zip(out["id"], out["internal_transfer"]))
    assert flags == {"pp-1": True, "cd-1": True}


def test_amount_mismatch_does_not_match():
    df = _frame(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-08",
                description="Bank Deposit to PP Account",
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-49.99,
                booking_date="2026-05-08",
                recipient="PAYPAL EUROPE",
            ),
        ]
    )
    out = _flag(df)
    assert not out["internal_transfer"].any()


def test_date_outside_window_does_not_match():
    df = _frame(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-01",
                description="Bank Deposit to PP Account",
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-50.00,
                booking_date="2026-05-10",
                recipient="PAYPAL EUROPE",
            ),
        ]
    )
    out = _flag(df)
    assert not out["internal_transfer"].any()


def test_unrelated_comdirect_debit_is_untouched():
    """A non-PayPal Comdirect debit of the same amount must not match."""
    df = _frame(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-08",
                description="Bank Deposit to PP Account",
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-50.00,
                booking_date="2026-05-08",
                recipient="REWE Markt GmbH",
            ),
        ]
    )
    out = _flag(df)
    assert not out["internal_transfer"].any()


def test_each_side_consumed_once():
    """Two equal deposits + one matching posting → only one pair flagged."""
    df = _frame(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-08",
                description="Bank Deposit to PP Account",
            ),
            _row(
                id="pp-2",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-08",
                description="Bank Deposit to PP Account",
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-50.00,
                booking_date="2026-05-08",
                recipient="PAYPAL EUROPE",
            ),
        ]
    )
    out = _flag(df)
    flags = dict(zip(out["id"], out["internal_transfer"]))
    # The single Comdirect posting pairs with exactly one PayPal deposit.
    assert flags["cd-1"] is True or flags["cd-1"]
    assert sum(bool(v) for v in flags.values()) == 2


def test_empty_frame_is_noop():
    out = _flag(pd.DataFrame())
    assert out.empty


# ── P2c — Santander credit-card settlement ──────────────────────────────


def _santander(id_: str, amount: float, day: str, merchant: str = "Merchant"):
    return _row(
        id=id_,
        source=DataSource.SANTANDER_CC,
        amount=amount,
        booking_date=day,
        description=merchant,
    )


def test_santander_settlement_flags_only_the_comdirect_posting():
    """The Comdirect lump posting is flagged; the card charges stay un-flagged."""
    df = _frame(
        [
            _santander("s-1", -90.00, "2026-05-04", "REWE"),
            _santander("s-2", -60.00, "2026-05-18", "Amazon"),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-150.00,
                booking_date="2026-06-02",
                recipient="Santander Consumer Bank Kartenabrechnung",
            ),
        ]
    )
    out = _flag(df)
    flags = dict(zip(out["id"], out["internal_transfer"]))
    assert flags["cd-1"] is True or flags["cd-1"]
    # The real spend stays counted exactly once — the charges are not flagged.
    assert not flags["s-1"]
    assert not flags["s-2"]


def test_santander_sum_includes_refunds():
    """A mid-cycle refund nets against the charges before the sum match."""
    df = _frame(
        [
            _santander("s-1", -100.00, "2026-05-03"),
            _santander("s-2", -60.00, "2026-05-12"),
            _santander("s-3", 10.00, "2026-05-20", "Refund"),  # net = -150.00
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-150.00,
                booking_date="2026-06-01",
                recipient="SANTANDER KARTENABRECHNUNG",
            ),
        ]
    )
    out = _flag(df)
    assert dict(zip(out["id"], out["internal_transfer"]))["cd-1"]


def test_teilzahlung_does_not_match():
    """A partial / instalment payment ≠ the cycle sum → no false positive."""
    df = _frame(
        [
            _santander("s-1", -90.00, "2026-05-04"),
            _santander("s-2", -60.00, "2026-05-18"),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-50.00,  # partial payment, not the -150.00 cycle sum
                booking_date="2026-06-02",
                recipient="Santander Kartenabrechnung Teilzahlung",
            ),
        ]
    )
    out = _flag(df)
    assert not out["internal_transfer"].any()


def test_unrelated_comdirect_debit_not_named_santander_is_untouched():
    df = _frame(
        [
            _santander("s-1", -150.00, "2026-05-04"),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-150.00,
                booking_date="2026-06-02",
                recipient="REWE Markt GmbH",
            ),
        ]
    )
    out = _flag(df)
    assert not out["internal_transfer"].any()


# ── PayPal purchase → Comdirect "PAYPAL EUROPE" context enrichment ──────
#
# A PayPal-funded purchase appears twice: an anonymous Comdirect "PAYPAL
# (EUROPE) S.A.R.L." debit and the PayPal row that holds the real
# merchant. `_enrich_paypal_purchases` copies the merchant onto the bank
# line; `_flag_cross_source_transfers` then flags the PayPal duplicate.


_enrich = NormalizationPipeline._enrich_paypal_purchases


def _enrich_and_flag(rows: list[dict]) -> pd.DataFrame:
    """Run the two passes a purchase goes through — merchant enrichment,
    then the cross-source flagging that consumes its marker."""
    return _flag(_enrich(_frame(rows)))


def test_paypal_purchase_enriches_the_comdirect_line():
    out = _enrich_and_flag(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=-19.99,
                booking_date="2026-05-08",
                recipient="STEAMGAMES",
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-19.99,
                booking_date="2026-05-09",
                recipient="PAYPAL EUROPE SARL ET CIE",
                description="PAYPAL Lastschrift 1234567890",
            ),
        ]
    )
    by_id = {r["id"]: r for r in out.to_dict("records")}
    # the anonymous bank line now names the real merchant …
    assert by_id["cd-1"]["recipient"] == "STEAMGAMES"
    assert "STEAMGAMES" in by_id["cd-1"]["description"]
    # … keeping the original bank text for audit
    assert "1234567890" in by_id["cd-1"]["description"]
    # the PayPal row is the duplicate leg → flagged; the bank line stays counted
    assert by_id["pp-1"]["internal_transfer"]
    assert not by_id["cd-1"]["internal_transfer"]
    # the temp marker column is cleaned up
    assert "_pp_purchase_dup" not in out.columns


def test_ambiguous_purchases_are_not_enriched():
    """Two PayPal purchases of the same amount could both match the one
    bank debit — the matcher refuses to guess, so nothing is enriched."""
    out = _enrich_and_flag(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
            _row(id="pp-2", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="NETFLIX.COM"),
            _row(id="cd-1", source=DataSource.COMDIRECT, amount=-19.99,
                 booking_date="2026-05-09", recipient="PAYPAL EUROPE"),
        ]
    )
    by_id = {r["id"]: r for r in out.to_dict("records")}
    assert by_id["cd-1"]["recipient"] == "PAYPAL EUROPE"  # untouched
    assert not any(by_id[i]["internal_transfer"] for i in ("pp-1", "pp-2", "cd-1"))


def test_purchase_without_a_bank_match_is_kept_as_spend():
    """A balance-funded PayPal purchase has no Comdirect counterpart — it
    must stay counted, not be flagged as a transfer."""
    out = _enrich_and_flag(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
        ]
    )
    assert not out.to_dict("records")[0]["internal_transfer"]


def test_purchase_outside_date_window_does_not_match():
    out = _enrich_and_flag(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-01", recipient="STEAMGAMES"),
            _row(id="cd-1", source=DataSource.COMDIRECT, amount=-19.99,
                 booking_date="2026-05-10", recipient="PAYPAL EUROPE"),
        ]
    )
    by_id = {r["id"]: r for r in out.to_dict("records")}
    assert by_id["cd-1"]["recipient"] == "PAYPAL EUROPE"
    assert not by_id["pp-1"]["internal_transfer"]


def test_enrichment_marker_survives_internal_transfer_reset():
    """`_flag_internal_transfers` rebuilds the `internal_transfer` column;
    the enrichment marker must outlive that so the purchase is still
    flagged by the later cross-source pass."""
    df = _enrich(
        _frame(
            [
                _row(id="pp-1", source=DataSource.PAYPAL, amount=-19.99,
                     booking_date="2026-05-08", recipient="STEAMGAMES"),
                _row(id="cd-1", source=DataSource.COMDIRECT, amount=-19.99,
                     booking_date="2026-05-09", recipient="PAYPAL EUROPE"),
            ]
        )
    )
    df = NormalizationPipeline._flag_internal_transfers(df, own_ibans=set())
    out = _flag(df)
    assert {r["id"]: r for r in out.to_dict("records")}["pp-1"]["internal_transfer"]


def test_topup_and_purchase_do_not_interfere():
    """A bank top-up and an unrelated purchase in one frame: the top-up
    flags both sides, the purchase flags only the PayPal duplicate."""
    out = _enrich_and_flag(
        [
            _row(id="pp-dep", source=DataSource.PAYPAL, amount=50.00,
                 booking_date="2026-05-02",
                 description="Bank Deposit to PP Account"),
            _row(id="cd-dep", source=DataSource.COMDIRECT, amount=-50.00,
                 booking_date="2026-05-02", recipient="PAYPAL EUROPE"),
            _row(id="pp-buy", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
            _row(id="cd-buy", source=DataSource.COMDIRECT, amount=-19.99,
                 booking_date="2026-05-09", recipient="PAYPAL EUROPE"),
        ]
    )
    by_id = {r["id"]: r for r in out.to_dict("records")}
    assert by_id["pp-dep"]["internal_transfer"] and by_id["cd-dep"]["internal_transfer"]
    assert by_id["pp-buy"]["internal_transfer"]
    assert not by_id["cd-buy"]["internal_transfer"]
    assert by_id["cd-buy"]["recipient"] == "STEAMGAMES"


# ── _flag_internal_transfers — IBAN-less rows are never own-account legs ─
#
# The IBAN matcher pairs opposite-sign, equal-amount rows. A PayPal
# payment and its same-amount refund a day apart look exactly like such
# a pair — but PayPal (and Santander credit-card) rows carry no IBAN and
# cannot be legs of an own-account transfer. Regression for the
# false-positive found during the first dev import.

_flag_internal = NormalizationPipeline._flag_internal_transfers


def test_paypal_payment_refund_pair_not_flagged_internal_transfer():
    """A PayPal payment and its same-amount refund one day apart must not
    be mistaken for an internal transfer — PayPal rows have no IBAN."""
    out = _flag_internal(
        _frame(
            [
                _row(id="pp-pay", source=DataSource.PAYPAL, amount=-24.99,
                     booking_date="2026-04-02", recipient="Some Merchant"),
                _row(id="pp-refund", source=DataSource.PAYPAL, amount=24.99,
                     booking_date="2026-04-03", description="Rückzahlung"),
            ]
        ),
        own_ibans=set(),
    )
    assert not out["internal_transfer"].any()


def test_iban_carrying_transfer_pair_is_still_flagged():
    """The guard only excludes IBAN-less rows — a genuine own-account
    transfer (both legs carry an IBAN) still pairs, even with no
    `own_ibans` configured."""
    out = _flag_internal(
        _frame(
            [
                _row(id="cd-out", source=DataSource.COMDIRECT, amount=-200.00,
                     booking_date="2026-04-02",
                     sender_iban="DE00000000000000000001"),
                _row(id="cd-in", source=DataSource.COMDIRECT, amount=200.00,
                     booking_date="2026-04-02",
                     recipient_iban="DE00000000000000000002"),
            ]
        ),
        own_ibans=set(),
    )
    assert out["internal_transfer"].all()
