"""Contract tests for the bounded read-only accounting report."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    TransactionLink,
)
from src.services.accounting_report import accounting_report

PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 8, 31)
AS_OF = date(2026, 8, 26)


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    RawTransaction.__table__.create(engine)
    NormalizedTransaction.__table__.create(engine)
    TransactionLink.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _add_transaction(
    db: Session,
    key: str,
    *,
    owner: str,
    source: DataSource = DataSource.COMDIRECT,
    amount: str = "-1.00",
    created_at: datetime = datetime(2026, 8, 25, tzinfo=timezone.utc),
    internal_transfer: bool = False,
    is_refund: bool = False,
    superseded_by: str | None = None,
) -> NormalizedTransaction:
    raw_hash = key.encode().hex().ljust(64, "0")[:64]
    db.add(
        RawTransaction(
            content_hash=raw_hash,
            source=source,
            raw_data={"owner_user_id": owner, "fixture": True},
            created_at=created_at,
            superseded_by=superseded_by,
        )
    )
    tx = NormalizedTransaction(
        id=key,
        raw_content_hash=raw_hash,
        source=source,
        booking_date=date(2026, 8, 15),
        valuation_date=date(2026, 8, 15),
        amount=Decimal(amount),
        currency="EUR",
        internal_transfer=internal_transfer,
        is_refund=is_refund,
    )
    db.add(tx)
    return tx


def _report(db: Session, owner: str, *sources: DataSource):
    db.commit()
    return accounting_report(
        db,
        owner_user_id=owner,
        start=PERIOD_START,
        end=PERIOD_END,
        sources=sources or (DataSource.COMDIRECT,),
        as_of=AS_OF,
        freshness_days=7,
    )


def test_report_isolates_owner_and_selects_only_active_raw_revision(db):
    new_hash = "new".encode().hex().ljust(64, "0")[:64]
    _add_transaction(db, "old", owner="owner-a", amount="-99.00", superseded_by=new_hash)
    _add_transaction(db, "new", owner="owner-a", amount="-10.00")
    _add_transaction(db, "other-owner", owner="owner-b", amount="-500.00")

    report = _report(db, "owner-a")

    assert report["owner_user_id"] == "owner-a"
    assert report["transaction_count"] == 1
    assert report["classes"]["expense"]["outflow"] == Decimal("10.00")


def test_unique_exact_settlement_chain_excludes_only_parent(db):
    _add_transaction(
        db, "parent", owner="owner-a", amount="-30.00", internal_transfer=True
    )
    _add_transaction(db, "child-a", owner="owner-a", source=DataSource.PAYPAL, amount="-10.00")
    _add_transaction(db, "child-b", owner="owner-a", source=DataSource.PAYPAL, amount="-20.00")
    db.add_all(
        [
            TransactionLink(
                id="link-a",
                parent_transaction_id="parent",
                child_transaction_id="child-a",
                link_type="paypal_aggregate",
            ),
            TransactionLink(
                id="link-b",
                parent_transaction_id="parent",
                child_transaction_id="child-b",
                link_type="paypal_aggregate",
            ),
        ]
    )

    report = _report(db, "owner-a", DataSource.COMDIRECT, DataSource.PAYPAL)

    assert report["classes"]["settlement_parent_excluded"]["transaction_count"] == 1
    assert report["classes"]["expense"]["transaction_count"] == 2
    assert report["settlement_ambiguities"] == []


def test_non_unique_settlement_chain_stays_visible_as_ambiguity(db):
    for parent in ("parent-a", "parent-b"):
        _add_transaction(
            db, parent, owner="owner-a", amount="-10.00", internal_transfer=True
        )
        db.add(
            TransactionLink(
                id=f"link-{parent}",
                parent_transaction_id=parent,
                child_transaction_id="child",
                link_type="paypal_aggregate",
            )
        )
    _add_transaction(db, "child", owner="owner-a", source=DataSource.PAYPAL, amount="-10.00")

    report = _report(db, "owner-a", DataSource.COMDIRECT, DataSource.PAYPAL)

    assert report["classes"]["ambiguous"]["transaction_count"] == 3
    assert report["classes"]["settlement_parent_excluded"]["transaction_count"] == 0
    assert all(
        "child_has_multiple_parents" in item["reasons"]
        for item in report["settlement_ambiguities"]
    )


def test_totals_are_a_mutually_exclusive_partition(db):
    _add_transaction(db, "expense", owner="owner-a", amount="-2.00")
    _add_transaction(db, "refund", owner="owner-a", amount="1.00", is_refund=True)
    _add_transaction(db, "income", owner="owner-a", amount="3.00")
    _add_transaction(
        db, "transfer", owner="owner-a", amount="-4.00", internal_transfer=True
    )
    _add_transaction(db, "zero", owner="owner-a", amount="0.00")

    report = _report(db, "owner-a")

    assert sum(item["transaction_count"] for item in report["classes"].values()) == 5
    assert report["gross_cash_outflow"] == Decimal("6.00")
    assert report["partition_outflow"] == Decimal("6.00")
    assert report["partition_difference"] == Decimal("0.00")


def test_stale_and_missing_manual_sources_block_readiness_and_completeness(db):
    _add_transaction(
        db,
        "stale-paypal",
        owner="owner-a",
        source=DataSource.PAYPAL,
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    report = _report(db, "owner-a", DataSource.PAYPAL, DataSource.SANTANDER_CC)
    states = {item["source"]: item["state"] for item in report["source_coverage"]["sources"]}

    assert states == {"paypal": "stale", "santander_cc": "missing"}
    assert report["analysis_state"] == "incomplete_sources"
    assert report["source_coverage"]["freshness_ok"] is False
    assert report["source_coverage"]["complete"] is False
    assert report["can_claim_complete"] is False
