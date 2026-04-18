"""Tests for depot persistence (src.normalization.depot_ingest)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import (
    Depot,
    DepotTransaction,
    Instrument,
    PortfolioSnapshot,
    Position,
)
from src.normalization.depot_ingest import capture_snapshot, ingest_depots


def _sample_payload() -> dict:
    return {
        "accounts": [],
        "transactions": {},
        "depots": [
            {
                "depotId": "DEPOT_TEST_001",
                "clientId": "CLIENT_001",
                "depotType": {"text": "Standard-Depot"},
                "currency": "EUR",
            }
        ],
        "depot_positions": {
            "DEPOT_TEST_001": [
                {
                    "instrument": {
                        "isin": "DE0000000000",
                        "wkn": "000000",
                        "name": "Doe AG",
                        "instrumentType": "SHARE",
                    },
                    "quantity": {"value": "10"},
                    "currentPrice": {"price": {"value": "100.50"}},
                    "currentValue": {"value": "1005.00", "unit": "EUR"},
                    "purchaseValue": {"value": "900.00", "unit": "EUR"},
                    "prevDayPrice": {"price": {"value": "99.00"}},
                    "prevDayValue": {"value": "990.00"},
                },
                {
                    "instrument": {
                        "isin": "DE0000000001",
                        "wkn": "000001",
                        "name": "Doe ETF",
                        "instrumentType": "FUND",
                    },
                    "quantity": {"value": "5"},
                    "currentPrice": {"price": {"value": "200.00"}},
                    "currentValue": {"value": "1000.00"},
                    "purchaseValue": {"value": "850.00"},
                },
            ]
        },
        "depot_transactions": {
            "DEPOT_TEST_001": [
                {
                    "transactionId": "TX_BUY_001",
                    "bookingDate": "2026-03-15",
                    "transactionType": "BUY",
                    "instrument": {"isin": "DE0000000000", "wkn": "000000", "name": "Doe AG"},
                    "quantity": {"value": "10"},
                    "price": {"value": "90.00"},
                    "transactionValue": {"value": "900.00", "unit": "EUR"},
                },
                {
                    "transactionId": "TX_DIV_001",
                    "bookingDate": "2026-02-10",
                    "transactionType": "DIVIDEND",
                    "instrument": {"isin": "DE0000000000", "wkn": "000000", "name": "Doe AG"},
                    "quantity": {"value": "0"},
                    "price": {"value": "0"},
                    "transactionValue": {"value": "25.00", "unit": "EUR"},
                },
            ]
        },
    }


def test_ingest_depots_writes_all_entities(db_session: Session):
    stats = ingest_depots(db_session, _sample_payload())

    assert stats == {
        "depots": 1,
        "instruments": 2,
        "positions": 2,
        "transactions": 2,
    }

    depot = db_session.get(Depot, "DEPOT_TEST_001")
    assert depot is not None
    assert depot.depot_type == "Standard-Depot"

    instruments = db_session.execute(select(Instrument)).scalars().all()
    assert {i.isin for i in instruments} == {"DE0000000000", "DE0000000001"}

    positions = (
        db_session.execute(select(Position).where(Position.depot_id == "DEPOT_TEST_001"))
        .scalars()
        .all()
    )
    assert len(positions) == 2
    pos_by_isin = {p.isin: p for p in positions}
    assert pos_by_isin["DE0000000000"].current_value == Decimal("1005.00")
    assert pos_by_isin["DE0000000000"].prev_day_value == Decimal("990.00")

    txs = (
        db_session.execute(
            select(DepotTransaction).where(DepotTransaction.depot_id == "DEPOT_TEST_001")
        )
        .scalars()
        .all()
    )
    assert {t.transaction_type for t in txs} == {"BUY", "DIVIDEND"}


def test_ingest_is_idempotent_on_second_run(db_session: Session):
    payload = _sample_payload()
    ingest_depots(db_session, payload)
    ingest_depots(db_session, payload)
    # Positions are delete-insert per depot, transactions use ON CONFLICT DO
    # NOTHING — what matters is the DB has no duplicates after the second run.
    tx_count = db_session.execute(select(DepotTransaction)).scalars().all()
    assert len(tx_count) == 2

    positions = db_session.execute(select(Position)).scalars().all()
    assert len(positions) == 2


def test_stale_position_is_removed_on_refresh(db_session: Session):
    ingest_depots(db_session, _sample_payload())

    # second sync: ETF position gone
    payload = _sample_payload()
    payload["depot_positions"]["DEPOT_TEST_001"] = [
        payload["depot_positions"]["DEPOT_TEST_001"][0]
    ]
    ingest_depots(db_session, payload)

    remaining = (
        db_session.execute(select(Position).where(Position.depot_id == "DEPOT_TEST_001"))
        .scalars()
        .all()
    )
    assert {p.isin for p in remaining} == {"DE0000000000"}


def test_capture_snapshot_writes_per_depot_and_aggregate(db_session: Session):
    ingest_depots(db_session, _sample_payload())
    rows_written = capture_snapshot(db_session, date(2026, 4, 18))
    assert rows_written == 2  # 1 depot + 1 aggregate

    snapshots = db_session.execute(select(PortfolioSnapshot)).scalars().all()
    assert len(snapshots) == 2

    depot_snap = next(s for s in snapshots if s.depot_id == "DEPOT_TEST_001")
    agg_snap = next(s for s in snapshots if s.depot_id is None)
    assert depot_snap.total_value == Decimal("2005.00")
    assert agg_snap.total_value == Decimal("2005.00")
    assert agg_snap.total_purchase_value == Decimal("1750.00")


def test_capture_snapshot_is_idempotent_per_day(db_session: Session):
    ingest_depots(db_session, _sample_payload())
    capture_snapshot(db_session, date(2026, 4, 18))
    capture_snapshot(db_session, date(2026, 4, 18))

    snapshots = db_session.execute(select(PortfolioSnapshot)).scalars().all()
    assert len(snapshots) == 2  # no duplicates (depot + aggregate)


def test_capture_snapshot_multiple_days_builds_timeseries(db_session: Session):
    ingest_depots(db_session, _sample_payload())
    capture_snapshot(db_session, date(2026, 4, 17))
    capture_snapshot(db_session, date(2026, 4, 18))

    aggregate = (
        db_session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.depot_id.is_(None))
            .order_by(PortfolioSnapshot.snapshot_date)
        )
        .scalars()
        .all()
    )
    assert [s.snapshot_date for s in aggregate] == [date(2026, 4, 17), date(2026, 4, 18)]
