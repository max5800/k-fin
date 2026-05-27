"""Tests for GET /transactions/{id}/links aggregate drilldown."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    TransactionLink,
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


def _add_tx(
    session: Session,
    *,
    tx_id: str,
    source: DataSource,
    amount: Decimal,
    recipient: str,
    internal_transfer: bool,
) -> None:
    raw_hash = tx_id.ljust(64, "0")[:64]
    session.add(
        RawTransaction(
            content_hash=raw_hash,
            source=source,
            external_id=tx_id,
            raw_data={"stub": True},
        )
    )
    session.add(
        NormalizedTransaction(
            id=tx_id,
            raw_content_hash=raw_hash,
            source=source,
            external_id=tx_id,
            booking_date=date(2026, 5, 9),
            valuation_date=date(2026, 5, 9),
            amount=amount,
            currency="EUR",
            sender="John Doe",
            recipient=recipient,
            description=recipient,
            is_recurring=False,
            is_outlier=False,
            internal_transfer=internal_transfer,
        )
    )


@pytest.fixture
def seed_links(db_engine):
    with Session(db_engine) as session:
        _add_tx(
            session,
            tx_id="parent",
            source=DataSource.COMDIRECT,
            amount=Decimal("-19.99"),
            recipient="PAYPAL EUROPE",
            internal_transfer=True,
        )
        _add_tx(
            session,
            tx_id="child",
            source=DataSource.PAYPAL,
            amount=Decimal("-19.99"),
            recipient="STEAMGAMES",
            internal_transfer=False,
        )
        session.add(
            TransactionLink(
                id="link-1",
                parent_transaction_id="parent",
                child_transaction_id="child",
                link_type="paypal_aggregate",
            )
        )
        session.commit()


def test_links_requires_auth(api_client, seed_links):
    resp = api_client.get("/api/v1/transactions/parent/links")
    assert resp.status_code == 401


def test_parent_returns_child_links(api_client, seed_links):
    resp = api_client.get("/api/v1/transactions/parent/links", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["transaction_id"] == "parent"
    assert body["parents"] == []
    assert len(body["children"]) == 1
    child = body["children"][0]
    assert child["link_type"] == "paypal_aggregate"
    assert child["transaction"]["id"] == "child"
    assert child["transaction"]["recipient"] == "STEAMGAMES"
    assert child["transaction"]["internal_transfer"] is False


def test_child_returns_parent_links(api_client, seed_links):
    resp = api_client.get("/api/v1/transactions/child/links", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["children"] == []
    assert len(body["parents"]) == 1
    parent = body["parents"][0]["transaction"]
    assert parent["id"] == "parent"
    assert parent["internal_transfer"] is True


def test_links_unknown_transaction_returns_404(api_client, seed_links):
    resp = api_client.get("/api/v1/transactions/missing/links", headers=AUTH)
    assert resp.status_code == 404
