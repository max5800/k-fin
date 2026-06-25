"""Integration tests for /depots and /portfolio routers."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    Depot,
    DepotTransaction,
    Instrument,
    PortfolioSnapshot,
    Position,
)

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def api_client(db_engine):
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


@pytest.fixture
def seeded(db_engine):
    with Session(db_engine) as s:
        s.add(Depot(depot_id="DEPOT_X", currency="EUR", depot_type="Standard"))
        s.add(
            Instrument(
                isin="DE0000000000",
                wkn="000000",
                name="Doe AG",
                instrument_type="SHARE",
                currency="EUR",
            )
        )
        s.add(
            Instrument(
                isin="DE0000000001",
                wkn="000001",
                name="Doe ETF",
                instrument_type="FUND",
                currency="EUR",
            )
        )
        s.commit()

        s.add(
            Position(
                depot_id="DEPOT_X",
                isin="DE0000000000",
                quantity=Decimal("10"),
                current_price=Decimal("100"),
                current_value=Decimal("1000"),
                purchase_value=Decimal("800"),
                prev_day_price=Decimal("99"),
                prev_day_value=Decimal("990"),
                currency="EUR",
            )
        )
        s.add(
            Position(
                depot_id="DEPOT_X",
                isin="DE0000000001",
                quantity=Decimal("5"),
                current_price=Decimal("200"),
                current_value=Decimal("1000"),
                purchase_value=Decimal("900"),
                currency="EUR",
            )
        )
        s.add(
            DepotTransaction(
                transaction_id="TX_DIV_1",
                depot_id="DEPOT_X",
                isin="DE0000000000",
                booking_date=date.today() - timedelta(days=60),
                transaction_type="DIVIDEND",
                quantity=Decimal("0"),
                price=Decimal("0"),
                amount=Decimal("40"),
                currency="EUR",
            )
        )
        s.add(
            PortfolioSnapshot(
                depot_id=None,
                snapshot_date=date.today() - timedelta(days=2),
                total_value=Decimal("1950"),
                total_purchase_value=Decimal("1700"),
                captured_at=datetime.now(timezone.utc),
            )
        )
        s.add(
            PortfolioSnapshot(
                depot_id=None,
                snapshot_date=date.today(),
                total_value=Decimal("2000"),
                total_purchase_value=Decimal("1700"),
                captured_at=datetime.now(timezone.utc),
            )
        )
        s.commit()


def test_auth_required(api_client):
    assert api_client.get("/api/v1/portfolio/summary").status_code == 401
    assert api_client.get("/api/v1/portfolio/home").status_code == 401
    assert api_client.get("/api/v1/portfolio/activities").status_code == 401
    assert api_client.get("/api/v1/depots").status_code == 401


def test_list_depots(api_client, seeded):
    r = api_client.get("/api/v1/depots", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["depot_id"] == "DEPOT_X"
    assert Decimal(rows[0]["total_value"]) == Decimal("2000")
    assert rows[0]["positions_count"] == 2


def test_positions_includes_weight_and_daily_pnl(api_client, seeded):
    r = api_client.get("/api/v1/depots/DEPOT_X/positions", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2

    # Position DE0000000000 has prev_day_value=990, current_value=1000 → +10 abs
    share = next(p for p in rows if p["instrument"]["isin"] == "DE0000000000")
    assert Decimal(share["daily_pnl_abs"]) == Decimal("10")
    # weights sum to ~100%
    total_weight = sum(Decimal(p["weight_pct"]) for p in rows)
    assert abs(total_weight - Decimal("100")) < Decimal("0.01")


def test_depot_not_found(api_client, seeded):
    r = api_client.get("/api/v1/depots/DOES_NOT_EXIST/positions", headers=AUTH)
    assert r.status_code == 404


def test_portfolio_summary(api_client, seeded):
    r = api_client.get("/api/v1/portfolio/summary", headers=AUTH)
    assert r.status_code == 200
    s = r.json()
    assert Decimal(s["total_value"]) == Decimal("2000")
    assert Decimal(s["total_purchase_value"]) == Decimal("1700")
    assert Decimal(s["total_pnl_abs"]) == Decimal("300")
    # daily_ref = 990 (only share has prev_day data), daily_current = 1000
    assert Decimal(s["daily_pnl_abs"]) == Decimal("10")
    # dividend yield = 40 / 2000 * 100 = 2 %
    assert Decimal(s["dividend_yield_pct"]) == Decimal("2")
    assert s["positions_count"] == 2
    assert s["depots_count"] == 1


def test_portfolio_allocation_drops_empty_buckets(api_client, seeded):
    r = api_client.get("/api/v1/portfolio/allocation", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    buckets = {row["bucket"] for row in rows}
    assert buckets == {"Aktien", "ETFs"}
    total_share = sum(Decimal(row["share_pct"]) for row in rows)
    assert abs(total_share - Decimal("100")) < Decimal("0.01")


def test_portfolio_performance_returns_series(api_client, seeded):
    r = api_client.get("/api/v1/portfolio/performance?range=1M", headers=AUTH)
    assert r.status_code == 200
    series = r.json()
    assert len(series) == 2
    assert Decimal(series[-1]["total_value"]) == Decimal("2000")


def test_portfolio_activities_returns_depot_feed(api_client, seeded):
    r = api_client.get("/api/v1/portfolio/activities?limit=5", headers=AUTH)
    assert r.status_code == 200
    body = r.json()

    assert body["total"] == 1
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert len(body["items"]) == 1

    activity = body["items"][0]
    assert activity["transaction_id"] == "TX_DIV_1"
    assert activity["instrument_name"] == "Doe AG"
    assert activity["transaction_type"] == "DIVIDEND"
    assert Decimal(activity["amount"]) == Decimal("40")


def test_portfolio_activities_filters_by_isin(api_client, seeded):
    r = api_client.get(
        "/api/v1/portfolio/activities?isin=DE0000000001",
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_portfolio_home_bundles_parqet_home_data(api_client, seeded):
    r = api_client.get("/api/v1/portfolio/home?range=1M&activity_limit=3", headers=AUTH)
    assert r.status_code == 200
    body = r.json()

    assert Decimal(body["summary"]["total_value"]) == Decimal("2000")
    assert {row["bucket"] for row in body["allocation"]} == {"Aktien", "ETFs"}
    assert len(body["performance"]) == 2

    assert len(body["activities"]) == 1
    activity = body["activities"][0]
    assert activity["transaction_id"] == "TX_DIV_1"
    assert activity["instrument_name"] == "Doe AG"
    assert activity["transaction_type"] == "DIVIDEND"
    assert Decimal(activity["amount"]) == Decimal("40")


def test_portfolio_home_allows_empty_activity_slice(api_client, seeded):
    r = api_client.get("/api/v1/portfolio/home?activity_limit=0", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["activities"] == []


def test_empty_db_returns_zero_summary(api_client):
    r = api_client.get("/api/v1/portfolio/summary", headers=AUTH)
    assert r.status_code == 200
    s = r.json()
    assert Decimal(s["total_value"]) == Decimal("0")
    assert s["positions_count"] == 0
    assert s["depots_count"] == 0


def test_empty_db_returns_empty_lists(api_client):
    assert api_client.get("/api/v1/depots", headers=AUTH).json() == []
    assert api_client.get("/api/v1/portfolio/allocation", headers=AUTH).json() == []
    assert api_client.get("/api/v1/portfolio/performance", headers=AUTH).json() == []
