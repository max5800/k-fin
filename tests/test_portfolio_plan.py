"""Tests for portfolio planning endpoints (savings plans + target allocation)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import Depot, Instrument, Position

AUTH = {"Authorization": "Bearer test-secret"}


def _client(db_engine) -> TestClient:
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)


def _seed_portfolio(db_engine) -> None:
    with Session(db_engine) as session:
        session.add(Depot(depot_id="depot-1", currency="EUR"))
        session.add(
            Instrument(
                isin="IE00BK5BQT80",
                wkn="A2PKXG",
                name="Vanguard FTSE All-World U.ETF Reg. Shs USD Acc. oN",
                currency="EUR",
            )
        )
        session.add(
            Instrument(
                isin="US02209S1033",
                wkn="200417",
                name="Altria Group Inc.",
                currency="EUR",
            )
        )
        session.flush()
        session.add(
            Position(
                depot_id="depot-1",
                isin="IE00BK5BQT80",
                quantity=Decimal("10"),
                current_price=Decimal("100"),
                current_value=Decimal("1000"),
                purchase_value=Decimal("900"),
                currency="EUR",
                as_of=datetime(2026, 6, 9, tzinfo=timezone.utc),
            )
        )
        session.add(
            Position(
                depot_id="depot-1",
                isin="US02209S1033",
                quantity=Decimal("1"),
                current_price=Decimal("100"),
                current_value=Decimal("100"),
                purchase_value=Decimal("80"),
                currency="EUR",
                as_of=datetime(2026, 6, 9, tzinfo=timezone.utc),
            )
        )
        session.commit()


def test_upsert_savings_plan_and_target_report(db_engine):
    _seed_portfolio(db_engine)
    client = _client(db_engine)

    resp = client.put(
        "/api/v1/portfolio/savings-plans/IE00BK5BQT80",
        headers=AUTH,
        json={
            "amount": "300.00",
            "currency": "EUR",
            "interval": "monthly",
            "start_date": "2026-06-01",
            "active": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["amount"] == "300.00"
    assert resp.json()["instrument"]["wkn"] == "A2PKXG"

    resp = client.put(
        "/api/v1/portfolio/targets/isin/IE00BK5BQT80",
        headers=AUTH,
        json={"target_weight_pct": "90.00", "active": True},
    )
    assert resp.status_code == 200

    report = client.get("/api/v1/portfolio/plan-report", headers=AUTH)
    assert report.status_code == 200
    data = report.json()
    assert data["total_value"] == "1100.00"
    assert data["active_monthly_savings_amount"] == "300.00"
    etf = next(p for p in data["positions"] if p["isin"] == "IE00BK5BQT80")
    assert etf["active_savings_amount"] == "300.00"
    assert etf["looks_like_etf"] is True


def test_plan_report_flags_active_single_stock_savings_plan_over_cap(db_engine):
    _seed_portfolio(db_engine)
    client = _client(db_engine)

    resp = client.put(
        "/api/v1/portfolio/savings-plans/US02209S1033",
        headers=AUTH,
        json={
            "amount": "50.00",
            "interval": "monthly",
            "start_date": "2026-06-01",
            "active": True,
        },
    )
    assert resp.status_code == 200

    report = client.get(
        "/api/v1/portfolio/plan-report?single_position_cap_pct=5",
        headers=AUTH,
    )
    assert report.status_code == 200
    data = report.json()
    altria = next(p for p in data["positions"] if p["isin"] == "US02209S1033")
    assert altria["over_single_position_cap"] is True
    assert data["suggestions"][0]["action"] == "pause_savings_plan"
