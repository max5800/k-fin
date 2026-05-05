"""Tests for GET /api/v1/transactions/export — CSV/JSON streaming export."""

from __future__ import annotations

import csv
import io
import json
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
    Tag,
    TransactionTag,
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
def seed_export_data(db_engine):
    with Session(db_engine) as s:
        s.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        s.add(Tag(id="important", name="Wichtig"))
        s.add(Tag(id="review", name="Pruefen"))

        s.add(RawTransaction(content_hash="a" * 64, comdirect_id="CD001", raw_data={"stub": True}))
        s.add(RawTransaction(content_hash="b" * 64, comdirect_id="CD002", raw_data={"stub": True}))
        s.flush()

        s.add(
            NormalizedTransaction(
                id="txnE1",
                raw_content_hash="a" * 64,
                comdirect_id="CD001",
                booking_date=date(2026, 4, 10),
                valuation_date=date(2026, 4, 11),
                amount=Decimal("-42.50"),
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
        s.add(
            NormalizedTransaction(
                id="txnE2",
                raw_content_hash="b" * 64,
                comdirect_id="CD002",
                booking_date=date(2026, 3, 1),
                valuation_date=date(2026, 3, 1),
                amount=Decimal("3500.00"),
                currency="EUR",
                sender="Arbeitgeber AG",
                recipient="John Doe",
                description="Gehalt",
                is_recurring=True,
                is_outlier=False,
                internal_transfer=False,
            )
        )
        s.add(TransactionTag(transaction_id="txnE1", tag_id="important"))
        s.add(TransactionTag(transaction_id="txnE1", tag_id="review"))
        s.commit()


def test_export_requires_auth(api_client):
    resp = api_client.get("/api/v1/transactions/export")
    assert resp.status_code == 401


def test_csv_export_default_format(api_client, seed_export_data):
    resp = api_client.get("/api/v1/transactions/export", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    cd = resp.headers["content-disposition"]
    assert "attachment" in cd
    assert "transactions-" in cd and ".csv" in cd

    reader = csv.DictReader(io.StringIO(resp.text))
    assert reader.fieldnames == [
        "booking_date",
        "value_date",
        "description",
        "counterparty",
        "amount",
        "currency",
        "category",
        "tags",
    ]
    rows = list(reader)
    assert len(rows) == 2

    # Ordered newest first by booking_date, so txnE1 (April) is first.
    rewe_row = rows[0]
    assert rewe_row["booking_date"] == "2026-04-10"
    assert rewe_row["value_date"] == "2026-04-11"
    assert rewe_row["counterparty"] == "REWE"
    assert rewe_row["description"] == "Einkauf REWE"
    assert rewe_row["amount"] == "-42.50"
    assert rewe_row["currency"] == "EUR"
    assert rewe_row["category"] == "Lebensmittel"
    # Tags joined comma-separated; order is insertion order.
    assert set(rewe_row["tags"].split(",")) == {"Wichtig", "Pruefen"}


def test_csv_export_filters_by_category(api_client, seed_export_data):
    resp = api_client.get(
        "/api/v1/transactions/export?category_id=groceries", headers=AUTH
    )
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    assert rows[0]["counterparty"] == "REWE"


def test_csv_export_filters_by_date_range(api_client, seed_export_data):
    resp = api_client.get(
        "/api/v1/transactions/export?date_from=2026-04-01&date_to=2026-04-30",
        headers=AUTH,
    )
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    assert rows[0]["booking_date"] == "2026-04-10"


def test_csv_export_search(api_client, seed_export_data):
    resp = api_client.get("/api/v1/transactions/export?search=REWE", headers=AUTH)
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1


def test_csv_export_empty_returns_header_only(api_client, seed_export_data):
    resp = api_client.get(
        "/api/v1/transactions/export?search=NoSuchString", headers=AUTH
    )
    assert resp.status_code == 200
    text = resp.text.strip()
    # Just the header row, no data.
    assert text.startswith(
        "booking_date,value_date,description,counterparty,amount,currency,category,tags"
    )
    assert "\n" not in text


def test_json_export_returns_array(api_client, seed_export_data):
    resp = api_client.get("/api/v1/transactions/export?format=json", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert ".json" in resp.headers["content-disposition"]

    body = json.loads(resp.text)
    assert isinstance(body, list)
    assert len(body) == 2
    first = body[0]
    assert first["counterparty"] == "REWE"
    assert first["amount"] == "-42.50"


def test_export_rejects_unknown_format(api_client, seed_export_data):
    resp = api_client.get("/api/v1/transactions/export?format=xml", headers=AUTH)
    assert resp.status_code == 422
