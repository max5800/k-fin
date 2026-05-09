"""Integration tests for the M11 instrument-price endpoints.

Covers PATCH ticker, POST backfill (happy path + missing ticker + provider
failure + idempotent re-run) and GET prices (empty + filtered).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.connector.yfinance_client import (
    PriceFetchError,
    PricePoint,
)
from src.core.db.models import (
    Depot,
    Instrument,
    InstrumentPriceHistory,
    Position,
)

AUTH = {"Authorization": "Bearer test-secret"}

# Use a letter-tagged fake ISIN so gitleaks' \bDE\d{20}\b rule does not fire.
FAKE_ISIN = "DE0000000000"


class FakeProvider:
    """Minimal HistoryProvider fake driven by per-test setup."""

    def __init__(
        self,
        points: list[PricePoint] | None = None,
        error: Exception | None = None,
    ):
        self.points = points or []
        self.error = error
        self.calls: list[tuple[str, date, date]] = []

    def get_history(
        self, ticker: str, from_date: date, to_date: date
    ) -> list[PricePoint]:
        self.calls.append((ticker, from_date, to_date))
        if self.error is not None:
            raise self.error
        return [
            p for p in self.points if from_date <= p.price_date <= to_date
        ]


@pytest.fixture
def fake_provider():
    return FakeProvider()


@pytest.fixture
def api_client(db_engine, fake_provider):
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
        from src.api.app import create_app
        from src.api.routers.portfolio import get_history_provider

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_history_provider] = lambda: fake_provider
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


@pytest.fixture
def seeded(db_engine):
    """A single depot+instrument with a position; ticker not yet set."""
    with Session(db_engine) as s:
        s.add(Depot(depot_id="DEPOT_X", currency="EUR"))
        s.add(
            Instrument(
                isin=FAKE_ISIN,
                wkn="000000",
                name="Doe AG",
                instrument_type="SHARE",
                currency="EUR",
            )
        )
        s.commit()
        s.add(
            Position(
                depot_id="DEPOT_X",
                isin=FAKE_ISIN,
                quantity=Decimal("10"),
                current_price=Decimal("100"),
                current_value=Decimal("1000"),
                purchase_value=Decimal("800"),
                currency="EUR",
            )
        )
        s.commit()


def test_patch_instrument_sets_ticker(api_client, seeded, db_engine):
    r = api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["ticker_symbol"] == "SAP.DE"

    with Session(db_engine) as s:
        instr = s.get(Instrument, FAKE_ISIN)
        assert instr is not None
        assert instr.ticker_symbol == "SAP.DE"


def test_patch_instrument_blank_clears_ticker(api_client, seeded, db_engine):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=AUTH,
    )
    r = api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "   "},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["ticker_symbol"] is None


def test_patch_instrument_unknown_isin_404(api_client):
    r = api_client.patch(
        "/api/v1/portfolio/instruments/DE9999999999",
        json={"ticker_symbol": "X"},
        headers=AUTH,
    )
    assert r.status_code == 404


def test_backfill_requires_ticker(api_client, seeded):
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "ticker_symbol" in r.json()["detail"]


def test_backfill_inserts_points(api_client, seeded, fake_provider, db_engine):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=AUTH,
    )
    fake_provider.points = [
        PricePoint(date(2026, 4, 1), Decimal("100.50"), "EUR"),
        PricePoint(date(2026, 4, 2), Decimal("101.25"), "EUR"),
        PricePoint(date(2026, 4, 3), Decimal("102.00"), "EUR"),
    ]

    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-03"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched_points"] == 3
    assert body["inserted_points"] == 3
    assert body["skipped_existing"] == 0
    assert body["source"] == "yfinance"
    assert fake_provider.calls == [("SAP.DE", date(2026, 4, 1), date(2026, 4, 3))]

    with Session(db_engine) as s:
        rows = s.query(InstrumentPriceHistory).all()
        assert len(rows) == 3
        assert {r.price_date for r in rows} == {
            date(2026, 4, 1),
            date(2026, 4, 2),
            date(2026, 4, 3),
        }


def test_backfill_idempotent_on_overlap(
    api_client, seeded, fake_provider, db_engine
):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=AUTH,
    )
    fake_provider.points = [
        PricePoint(date(2026, 4, 1), Decimal("100.0"), "EUR"),
        PricePoint(date(2026, 4, 2), Decimal("101.0"), "EUR"),
    ]
    api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-02"},
        headers=AUTH,
    )

    # Second run with one new day → only that one inserted.
    fake_provider.points = [
        PricePoint(date(2026, 4, 1), Decimal("100.0"), "EUR"),
        PricePoint(date(2026, 4, 2), Decimal("101.0"), "EUR"),
        PricePoint(date(2026, 4, 3), Decimal("102.0"), "EUR"),
    ]
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-03"},
        headers=AUTH,
    )
    body = r.json()
    assert body["fetched_points"] == 3
    assert body["inserted_points"] == 1
    assert body["skipped_existing"] == 2

    with Session(db_engine) as s:
        assert s.query(InstrumentPriceHistory).count() == 3


def test_backfill_provider_error_returns_502(api_client, seeded, fake_provider):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "BOGUS.DE"},
        headers=AUTH,
    )
    fake_provider.error = PriceFetchError("rate limited")

    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=AUTH,
    )
    assert r.status_code == 502
    assert "rate limited" in r.json()["detail"]


def test_backfill_empty_response_succeeds_with_zero_inserts(
    api_client, seeded, fake_provider
):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "X.DE"},
        headers=AUTH,
    )
    fake_provider.points = []

    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fetched_points"] == 0
    assert body["inserted_points"] == 0


def test_backfill_rejects_inverted_range(api_client, seeded):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=AUTH,
    )
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-10", "to_date": "2026-04-01"},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_get_prices_empty(api_client, seeded):
    r = api_client.get(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/prices",
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json() == []


def test_get_prices_returns_sorted_filtered_series(api_client, seeded, db_engine):
    with Session(db_engine) as s:
        for d, close in [
            (date(2026, 3, 28), Decimal("99.0")),
            (date(2026, 4, 1), Decimal("100.0")),
            (date(2026, 4, 2), Decimal("101.0")),
            (date(2026, 4, 5), Decimal("102.0")),
        ]:
            s.add(
                InstrumentPriceHistory(
                    isin=FAKE_ISIN,
                    price_date=d,
                    close=close,
                    currency="EUR",
                    source="yfinance",
                )
            )
        s.commit()

    r = api_client.get(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/prices",
        params={"from": "2026-04-01", "to": "2026-04-04"},
        headers=AUTH,
    )
    assert r.status_code == 200
    series = r.json()
    assert [p["price_date"] for p in series] == ["2026-04-01", "2026-04-02"]
    assert Decimal(series[0]["close"]) == Decimal("100.0")
    assert series[0]["source"] == "yfinance"


def test_get_prices_unknown_isin_404(api_client, seeded):
    r = api_client.get(
        "/api/v1/portfolio/instruments/DE9999999999/prices",
        headers=AUTH,
    )
    assert r.status_code == 404


def test_endpoints_require_auth(api_client, seeded):
    assert (
        api_client.patch(
            f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
            json={"ticker_symbol": "X"},
        ).status_code
        == 401
    )
    assert (
        api_client.post(
            f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
            json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        ).status_code
        == 401
    )
    assert (
        api_client.get(
            f"/api/v1/portfolio/instruments/{FAKE_ISIN}/prices"
        ).status_code
        == 401
    )
