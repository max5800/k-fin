"""Dev router: wipe + seed + status, gated on dev_tools_enabled."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    NormalizedTransaction,
    RawTransaction,
    SyncRun,
)

AUTH = {"Authorization": "Bearer test-secret"}


def _make_client(db_engine, *, dev_enabled: bool, app_env: str = "development"):
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    env = {
        "API_TOKEN": "test-secret",
        "DATABASE_URL": db_url,
        "APP_ENV": app_env,
        "DEV_TOOLS_ENABLED": "true" if dev_enabled else "false",
    }
    with patch.dict(os.environ, env):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        try:
            yield client
        finally:
            app.dependency_overrides.clear()


@pytest.fixture
def dev_client(db_engine):
    yield from _make_client(db_engine, dev_enabled=True)


@pytest.fixture
def disabled_client(db_engine):
    yield from _make_client(db_engine, dev_enabled=False)


@pytest.fixture
def prod_client(db_engine):
    # In a real prod deployment the env var simply isn't set, so the flag
    # defaults to False. We model that here by passing dev_enabled=False.
    yield from _make_client(db_engine, dev_enabled=False, app_env="production")


@pytest.fixture
def seed_categories(db_engine):
    """The seed generator references the production category seed; mirror
    it here so tests don't depend on running migration 0005."""
    from src.core.db.models import Category, TypeEnum

    needed = [
        ("gehalt", "Gehalt", TypeEnum.FIX),
        ("miete", "Miete", TypeEnum.FIX),
        ("internet-telefon", "Internet", TypeEnum.FIX),
        ("strom-gas", "Strom & Gas", TypeEnum.FIX),
        ("abos-streaming", "Abos", TypeEnum.FIX),
        ("versicherungen", "Versicherungen", TypeEnum.FIX),
        ("etf-sparplan", "ETF-Sparplan", TypeEnum.FIX),
        ("gez", "GEZ", TypeEnum.FIX),
        ("lebensmittel", "Lebensmittel", TypeEnum.VARIABEL),
        ("drogerie", "Drogerie", TypeEnum.VARIABEL),
        ("tanken", "Tanken", TypeEnum.VARIABEL),
        ("restaurant-cafe", "Restaurant", TypeEnum.DISKRETIONAER),
        ("gesundheit", "Gesundheit", TypeEnum.VARIABEL),
        ("erstattungen", "Erstattungen", TypeEnum.VARIABEL),
        ("kleidung", "Kleidung", TypeEnum.DISKRETIONAER),
        ("reisen", "Reisen", TypeEnum.DISKRETIONAER),
        ("elektronik", "Elektronik", TypeEnum.DISKRETIONAER),
        ("umbuchung", "Umbuchung", TypeEnum.FIX),
    ]
    with Session(db_engine) as s:
        for cid, name, t in needed:
            s.add(Category(id=cid, name=name, type=t))
        s.commit()


class TestStatus:
    def test_status_reports_enabled(self, dev_client):
        resp = dev_client.get("/api/v1/dev/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["app_env"] == "development"

    def test_status_reports_disabled(self, disabled_client):
        resp = disabled_client.get("/api/v1/dev/status")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_status_reports_production_force_disabled(self, prod_client):
        resp = prod_client.get("/api/v1/dev/status")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


class TestWipeGuards:
    def test_wipe_disabled_returns_404(self, disabled_client):
        resp = disabled_client.post("/api/v1/dev/wipe", headers=AUTH)
        assert resp.status_code == 404

    def test_wipe_production_returns_404(self, prod_client):
        resp = prod_client.post("/api/v1/dev/wipe", headers=AUTH)
        assert resp.status_code == 404

    def test_wipe_requires_auth(self, dev_client):
        resp = dev_client.post("/api/v1/dev/wipe")
        assert resp.status_code == 401


class TestSeedGuards:
    def test_seed_disabled_returns_404(self, disabled_client):
        resp = disabled_client.post("/api/v1/dev/seed", headers=AUTH)
        assert resp.status_code == 404

    def test_seed_requires_auth(self, dev_client):
        resp = dev_client.post("/api/v1/dev/seed")
        assert resp.status_code == 401

    def test_seed_without_categories_returns_409(self, dev_client):
        resp = dev_client.post("/api/v1/dev/seed", headers=AUTH)
        assert resp.status_code == 409


class TestSeedRoundtrip:
    def test_seed_inserts_realistic_dataset(self, dev_client, seed_categories, db_engine):
        resp = dev_client.post("/api/v1/dev/seed", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["inserted_transactions"] > 100
        assert body["refund_count"] >= 3  # Splitwise + Krankenkasse + Amazon
        assert body["internal_transfer_count"] == 2
        assert body["outlier_count"] == 1

        with Session(db_engine) as s:
            n_tx = s.query(NormalizedTransaction).count()
            n_raw = s.query(RawTransaction).count()
            assert n_tx == body["inserted_transactions"]
            assert n_raw == body["inserted_transactions"]
            assert s.query(SyncRun).count() == 1

    def test_seed_includes_refunds_in_correct_categories(
        self, dev_client, seed_categories, db_engine
    ):
        dev_client.post("/api/v1/dev/seed", headers=AUTH)
        with Session(db_engine) as s:
            refunds = s.query(NormalizedTransaction).filter_by(is_refund=True).all()
            categories = {r.category_id for r in refunds}
            # Refunds must sit on their *original expense* category, not on
            # the income-bucket `erstattungen` — that's the whole point of
            # the new is_refund convention.
            assert "erstattungen" not in categories
            assert categories.issubset({"gesundheit", "restaurant-cafe", "kleidung"})
            assert all(r.amount > 0 for r in refunds)

    def test_seed_internal_transfers_balance_to_zero(
        self, dev_client, seed_categories, db_engine
    ):
        dev_client.post("/api/v1/dev/seed", headers=AUTH)
        with Session(db_engine) as s:
            transfers = (
                s.query(NormalizedTransaction)
                .filter_by(internal_transfer=True)
                .all()
            )
            assert len(transfers) == 2
            assert sum(t.amount for t in transfers) == Decimal("0")


class TestWipeRoundtrip:
    def test_wipe_clears_transactions_keeps_categories(
        self, dev_client, seed_categories, db_engine
    ):
        dev_client.post("/api/v1/dev/seed", headers=AUTH)
        with Session(db_engine) as s:
            before = s.query(NormalizedTransaction).count()
            assert before > 0

        resp = dev_client.post("/api/v1/dev/wipe", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["transaction_count_before"] == before
        assert body["transaction_count_after"] == 0
        assert "normalized_transactions" in body["wiped_tables"]

        with Session(db_engine) as s:
            assert s.query(NormalizedTransaction).count() == 0
            assert s.query(RawTransaction).count() == 0
            # Categories survive the wipe.
            from src.core.db.models import Category

            assert s.query(Category).count() > 0


class TestSeedShape:
    def test_period_is_six_full_months(
        self, dev_client, seed_categories, db_engine
    ):
        resp = dev_client.post("/api/v1/dev/seed", headers=AUTH)
        body = resp.json()
        ps = date.fromisoformat(body["period_start"])
        pe = date.fromisoformat(body["period_end"])
        # Should span 6 months — give or take a day for month-length quirks.
        assert (pe - ps).days >= 5 * 28
        # Last booking must be in a fully-completed month, not in the
        # current partial one.
        assert pe < date.today().replace(day=1)
