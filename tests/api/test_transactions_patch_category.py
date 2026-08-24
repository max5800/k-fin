"""Tests for PATCH /transactions/{id} — manual category set / clear.

The endpoint pre-dates the M10 "schreibende UI" sub-item; this suite
covers the manual-categorize contract: setting a category id, swapping
to a different one, and explicit clearing via JSON ``null``. The
absent-key vs. null-value distinction is load-bearing — the client UI
relies on it to mean "leave alone" vs "clear".
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    Category,
    NormalizedTransaction,
    RawTransaction,
    TypeEnum,
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
def seed(db_engine):
    """Seed two categories and one transaction (already categorised as
    ``groceries``) so tests can exercise both the swap and the clear
    branches without doing extra setup themselves.
    """
    with Session(db_engine) as s:
        s.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        s.add(Category(id="rent", name="Miete", type=TypeEnum.FIX))
        s.add(
            RawTransaction(
                content_hash="t1".ljust(64, "0"),
                external_id="CD1",
                raw_data={"stub": True},
            )
        )
        s.flush()
        s.add(
            NormalizedTransaction(
                id="t1".ljust(64, "0"),
                raw_content_hash="t1".ljust(64, "0"),
                external_id="CD1",
                booking_date=date(2026, 3, 15),
                valuation_date=date(2026, 3, 15),
                amount=Decimal("-10.00"),
                currency="EUR",
                sender="John Doe",
                recipient="REWE",
                description="Einkauf REWE",
                category_id="groceries",
                is_recurring=False,
                is_outlier=False,
                internal_transfer=False,
            )
        )
        s.commit()


TX_ID = "t1".ljust(64, "0")


class TestManualCategorize:
    def test_patch_requires_auth(self, api_client, seed):
        resp = api_client.patch(
            f"/api/v1/transactions/{TX_ID}", json={"category_id": "rent"}
        )
        assert resp.status_code == 401

    def test_set_category_to_existing(self, api_client, seed, db_engine):
        resp = api_client.patch(
            f"/api/v1/transactions/{TX_ID}",
            json={"category_id": "rent"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["category"]["id"] == "rent"

        with Session(db_engine) as s:
            tx = s.get(NormalizedTransaction, TX_ID)
            assert tx.category_id == "rent"
            assert tx.accounting_class == "reconciled_consumption"
            assert tx.accounting_version == 2

    def test_set_category_explicit_null_clears(self, api_client, seed, db_engine):
        # Distinguishing "{}" from '{"category_id": null}' is the whole
        # point of the manual-categorize endpoint — null must clear.
        resp = api_client.patch(
            f"/api/v1/transactions/{TX_ID}",
            json={"category_id": None},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["category"] is None

        with Session(db_engine) as s:
            tx = s.get(NormalizedTransaction, TX_ID)
            assert tx.category_id is None
            assert tx.accounting_class == "unresolved_ambiguous"
            assert tx.accounting_version == 2

    def test_omitted_key_does_not_change_category(
        self, api_client, seed, db_engine
    ):
        # Sending an empty body or any other field must NOT clear the
        # category — the absent-key branch is "no change". This is the
        # corollary of the explicit-null test above and what protects
        # the UI from accidentally clearing categories when only
        # touching tags / refund flags.
        resp = api_client.patch(
            f"/api/v1/transactions/{TX_ID}",
            json={"is_refund": False},
            headers=AUTH,
        )
        assert resp.status_code == 200

        with Session(db_engine) as s:
            tx = s.get(NormalizedTransaction, TX_ID)
            assert tx.category_id == "groceries"

    def test_set_unknown_category_returns_422(self, api_client, seed):
        resp = api_client.patch(
            f"/api/v1/transactions/{TX_ID}",
            json={"category_id": "does-not-exist"},
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_patch_404_for_missing_transaction(self, api_client, seed):
        resp = api_client.patch(
            "/api/v1/transactions/does-not-exist",
            json={"category_id": "rent"},
            headers=AUTH,
        )
        assert resp.status_code == 404
