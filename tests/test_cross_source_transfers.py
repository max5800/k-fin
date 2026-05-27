"""Cross-source internal-transfer matching tests (M16-P2b).

A PayPal "Bankgutschrift auf PayPal-Konto" credit and its Comdirect
"PAYPAL EUROPE" debit counterpart describe one money movement. The
pipeline must flag both `internal_transfer=True` so the savings-rate
maths counts the top-up once, not twice.

`test_topup_matching_uses_real_parser_output` drives the actual PayPal
CSV importer end-to-end, so these hand-built fixtures cannot drift from
the German `Beschreibung` label the parser really emits.
"""

from __future__ import annotations

import pandas as pd
from datetime import date
from decimal import Decimal
from sqlalchemy.orm import Session

from src.core.db.models import DataSource, NormalizedTransaction, RawTransaction, TransactionLink
from src.normalization.pipeline import NormalizationPipeline

_flag = NormalizationPipeline._flag_cross_source_transfers
_reconcile = NormalizationPipeline._reconcile_cross_source_transfers


def _row(**kw):
    base = {
        "id": kw["id"],
        "source": kw["source"],
        "amount": kw["amount"],
        "booking_date": kw["booking_date"],
        "description": kw.get("description"),
        "sender": kw.get("sender"),
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
                description="Bankgutschrift auf PayPal-Konto",
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
                description="Bankgutschrift auf PayPal-Konto",
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
                description="Bankgutschrift auf PayPal-Konto",
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
                description="Bankgutschrift auf PayPal-Konto",
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
                description="Bankgutschrift auf PayPal-Konto",
            ),
            _row(
                id="pp-2",
                source=DataSource.PAYPAL,
                amount=50.00,
                booking_date="2026-05-08",
                description="Bankgutschrift auf PayPal-Konto",
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
    out, links = _reconcile(df)
    flags = dict(zip(out["id"], out["internal_transfer"]))
    assert flags["cd-1"] is True or flags["cd-1"]
    # The real spend stays counted exactly once — the charges are not flagged.
    assert not flags["s-1"]
    assert not flags["s-2"]
    assert {link["child_transaction_id"] for link in links} == {"s-1", "s-2"}
    assert {link["parent_transaction_id"] for link in links} == {"cd-1"}


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
    out, links = _reconcile(df)
    assert dict(zip(out["id"], out["internal_transfer"]))["cd-1"]
    assert {link["child_transaction_id"] for link in links} == {"s-1", "s-2", "s-3"}


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


# ── PayPal purchase aggregate → detail links ────────────────────────────


def _reconcile_rows(rows: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    return _reconcile(_frame(rows))


def test_paypal_purchase_links_parent_and_keeps_child_countable():
    out, links = _reconcile_rows(
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
    assert by_id["cd-1"]["internal_transfer"]
    assert not by_id["pp-1"]["internal_transfer"]
    assert by_id["cd-1"]["recipient"] == "PAYPAL EUROPE SARL ET CIE"
    assert links == [
        {
            "id": links[0]["id"],
            "parent_transaction_id": "cd-1",
            "child_transaction_id": "pp-1",
            "link_type": "paypal_aggregate",
        }
    ]


def test_paypal_aggregate_exact_net_set_links_multiple_children():
    out, links = _reconcile_rows(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-20.00,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
            _row(id="pp-2", source=DataSource.PAYPAL, amount=-10.00,
                 booking_date="2026-05-08", recipient="NETFLIX.COM"),
            _row(id="pp-3", source=DataSource.PAYPAL, amount=5.00,
                 booking_date="2026-05-08", sender="Refund"),
            _row(id="cd-1", source=DataSource.COMDIRECT, amount=-25.00,
                 booking_date="2026-05-09", recipient="PAYPAL EUROPE"),
        ]
    )
    flags = dict(zip(out["id"], out["internal_transfer"]))
    assert flags["cd-1"]
    assert not flags["pp-1"]
    assert not flags["pp-2"]
    assert not flags["pp-3"]
    assert {link["child_transaction_id"] for link in links} == {"pp-1", "pp-2", "pp-3"}


def test_paypal_residual_amount_does_not_link():
    out, links = _reconcile_rows(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-20.00,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
            _row(id="cd-1", source=DataSource.COMDIRECT, amount=-25.00,
                 booking_date="2026-05-09", recipient="PAYPAL EUROPE"),
        ]
    )
    assert not out["internal_transfer"].any()
    assert links == []


def test_paypal_ambiguous_exact_subsets_do_not_link():
    out, links = _reconcile_rows(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
            _row(id="pp-2", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="NETFLIX.COM"),
            _row(id="cd-1", source=DataSource.COMDIRECT, amount=-19.99,
                 booking_date="2026-05-09", recipient="PAYPAL EUROPE"),
        ]
    )
    assert not out["internal_transfer"].any()
    assert links == []


def test_purchase_without_a_bank_match_is_kept_as_spend():
    out, links = _reconcile_rows(
        [
            _row(id="pp-1", source=DataSource.PAYPAL, amount=-19.99,
                 booking_date="2026-05-08", recipient="STEAMGAMES"),
        ]
    )
    assert not out.to_dict("records")[0]["internal_transfer"]
    assert links == []


def test_purchase_outside_date_window_does_not_match():
    out, links = _reconcile_rows(
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
    assert links == []


def test_topup_and_purchase_do_not_interfere():
    """A top-up pair does not consume a separate exact purchase aggregate."""
    out, links = _reconcile_rows(
        [
            _row(id="pp-dep", source=DataSource.PAYPAL, amount=50.00,
                 booking_date="2026-05-02",
                 description="Bankgutschrift auf PayPal-Konto"),
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
    assert not by_id["pp-buy"]["internal_transfer"]
    assert by_id["cd-buy"]["internal_transfer"]
    assert {link["parent_transaction_id"] for link in links} == {"cd-buy"}
    assert {link["child_transaction_id"] for link in links} == {"pp-buy"}


def _seed_normalized(session: Session, tx_id: str, amount: str = "-1.00") -> None:
    raw_hash = tx_id.ljust(64, "0")[:64]
    session.add(RawTransaction(content_hash=raw_hash, raw_data={"stub": True}))
    session.add(
        NormalizedTransaction(
            id=tx_id,
            raw_content_hash=raw_hash,
            source=DataSource.COMDIRECT,
            booking_date=date(2026, 5, 1),
            valuation_date=date(2026, 5, 1),
            amount=Decimal(amount),
            currency="EUR",
            sender="John Doe",
            recipient="Test Merchant",
            description="Test",
            is_recurring=False,
            is_outlier=False,
            internal_transfer=False,
        )
    )


def test_replace_transaction_links_cleans_stale_auto_links(db_engine):
    with Session(db_engine) as session:
        for tx_id in ("parent", "old-child", "new-child"):
            _seed_normalized(session, tx_id)
        session.add(
            TransactionLink(
                id="stale-auto",
                parent_transaction_id="parent",
                child_transaction_id="old-child",
                link_type="paypal_aggregate",
            )
        )
        session.add(
            TransactionLink(
                id="manual-link",
                parent_transaction_id="parent",
                child_transaction_id="old-child",
                link_type="manual",
            )
        )
        session.commit()

    with Session(db_engine) as session:
        NormalizationPipeline._replace_transaction_links(
            session,
            [
                {
                    "id": "fresh-auto",
                    "parent_transaction_id": "parent",
                    "child_transaction_id": "new-child",
                    "link_type": "paypal_aggregate",
                }
            ],
        )

    with Session(db_engine) as session:
        links = {
            link.id: (link.parent_transaction_id, link.child_transaction_id, link.link_type)
            for link in session.query(TransactionLink).all()
        }
    assert "stale-auto" not in links
    assert links["fresh-auto"] == ("parent", "new-child", "paypal_aggregate")
    assert links["manual-link"] == ("parent", "old-child", "manual")


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


# ── Regression: the matcher must agree with the real PayPal CSV parser ──
#
# `_match_paypal_topups` keys on the canonical `description`, which is the
# German `Beschreibung` the importer copies verbatim. An earlier revision
# matched the English literal "bank deposit" — dead code, since the
# importer never emits it, and the funding row was skipped outright. This
# test drives the real parser so the fixtures above cannot drift again.


def test_topup_matching_uses_real_parser_output():
    """The PayPal CSV importer keeps the bank-top-up row, and its
    canonical `description` is exactly what `_is_paypal_bank_deposit`
    matches — proving parser and matcher agree on the German label."""
    from src.normalization.paypal_csv import (
        parse_paypal_csv,
        paypal_csv_canonicalize,
    )

    csv_bytes = (
        "Datum,Beschreibung,Währung,Brutto,Transaktionscode,Name\r\n"
        '01.05.2026,Bankgutschrift auf PayPal-Konto,EUR,"50,00",TXN-FUND-1,\r\n'
    ).encode("utf-8")

    canonical = [paypal_csv_canonicalize(r) for r in parse_paypal_csv(csv_bytes)]
    # the funding row is imported (not skipped) — it is the top-up leg
    assert len(canonical) == 1
    topup = canonical[0]
    assert topup["amount"] > 0  # a credit — money arriving in PayPal

    df = _frame(
        [
            _row(
                id="pp-1",
                source=DataSource.PAYPAL,
                amount=topup["amount"],
                booking_date=topup["booking_date"],
                description=topup["description"],
            ),
            _row(
                id="cd-1",
                source=DataSource.COMDIRECT,
                amount=-50.00,
                booking_date=topup["booking_date"],
                recipient="PAYPAL EUROPE SARL ET CIE",
            ),
        ]
    )
    out = _flag(df)
    assert out["internal_transfer"].all()
