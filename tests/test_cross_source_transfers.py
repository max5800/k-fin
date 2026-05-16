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
