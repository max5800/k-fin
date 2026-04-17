"""Tests for the v1 Finance API endpoints.

Integration tests using testcontainers PostgreSQL.
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    Category,
    NormalizedTransaction,
    RawTransaction,
    RecurringPattern,
    Report,
    SyncRun,
    SyncSource,
    SyncStatus,
    Tag,
    TransactionTag,
    TypeEnum,
)

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def api_client(db_engine):
    """TestClient backed by a real Postgres database."""
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    with patch.dict(os.environ, {"API_TOKEN": "test-secret"}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


@pytest.fixture
def seed_data(db_engine):
    """Seed the database with test data."""
    with Session(db_engine) as s:
        # Categories
        s.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        s.add(Category(id="rent", name="Miete", type=TypeEnum.FIX))
        s.add(Category(id="fun", name="Freizeit", type=TypeEnum.DISKRETIONAER))

        # Tags
        s.add(Tag(id="important", name="Wichtig"))
        s.add(Tag(id="review", name="Prüfen"))

        # Recurring pattern
        pattern = RecurringPattern(
            id=1,
            recipient="Vermieter GmbH",
            avg_amount=Decimal("-850.00"),
            amount_stddev=Decimal("0.00"),
            first_seen_month=date(2025, 1, 1),
            last_seen_month=date(2026, 3, 1),
            occurrence_count=15,
        )
        s.add(pattern)

        # Sync run (must exist before Report FK)
        s.add(
            SyncRun(
                id="run001",
                source=SyncSource.NORMALIZE,
                status=SyncStatus.SUCCEEDED,
                started_at=datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 3, 15, 10, 1, tzinfo=timezone.utc),
                rows_processed=4,
            )
        )

        # Raw transactions (required by NormalizedTransaction FK)
        for hash_char, cid in [("a", "CD001"), ("b", "CD002"), ("c", None), ("d", None)]:
            s.add(
                RawTransaction(
                    content_hash=hash_char * 64,
                    comdirect_id=cid,
                    raw_data={"stub": True},
                )
            )

        s.flush()  # Ensure FKs are satisfied before dependents

        # Transactions
        s.add(
            NormalizedTransaction(
                id="txn001",
                raw_content_hash="a" * 64,
                comdirect_id="CD001",
                booking_date=date(2026, 3, 15),
                valuation_date=date(2026, 3, 15),
                amount=Decimal("-42.50"),
                currency="EUR",
                sender="John Doe",
                recipient="REWE",
                sender_iban="DE00000000000000000001",
                recipient_iban="DE00000000000000000002",
                description="Einkauf REWE",
                category_id="groceries",
                is_recurring=False,
                is_outlier=False,
                internal_transfer=False,
            )
        )
        s.add(
            NormalizedTransaction(
                id="txn002",
                raw_content_hash="b" * 64,
                comdirect_id="CD002",
                booking_date=date(2026, 3, 1),
                valuation_date=date(2026, 3, 1),
                amount=Decimal("-850.00"),
                currency="EUR",
                sender="John Doe",
                recipient="Vermieter GmbH",
                description="Miete März",
                category_id="rent",
                is_recurring=True,
                is_outlier=False,
                internal_transfer=False,
                recurring_pattern_id=1,
            )
        )
        s.add(
            NormalizedTransaction(
                id="txn003",
                raw_content_hash="c" * 64,
                booking_date=date(2026, 3, 10),
                valuation_date=date(2026, 3, 10),
                amount=Decimal("3500.00"),
                currency="EUR",
                sender="Arbeitgeber AG",
                recipient="John Doe",
                description="Gehalt März",
                is_recurring=True,
                is_outlier=False,
                internal_transfer=False,
            )
        )
        s.add(
            NormalizedTransaction(
                id="txn004",
                raw_content_hash="d" * 64,
                booking_date=date(2026, 2, 15),
                valuation_date=date(2026, 2, 15),
                amount=Decimal("-500.00"),
                currency="EUR",
                sender="John Doe",
                recipient="John Doe Tagesgeld",
                description="Umbuchung",
                is_recurring=False,
                is_outlier=False,
                internal_transfer=True,
            )
        )

        # Tag association
        s.add(TransactionTag(transaction_id="txn001", tag_id="important"))

        # Report
        s.add(
            Report(
                id="rpt001",
                report_type="weekly_analysis",
                title="weekly_analysis — 2026-W07",
                period_start=date(2026, 2, 9),
                period_end=date(2026, 2, 15),
                format="json",
                content={"summary": "Test report"},
            )
        )

        s.commit()


# --- Transactions ---


class TestTransactionEndpoints:
    def test_list_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/transactions")
        assert resp.status_code == 401

    def test_list_returns_transactions(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        assert len(data["items"]) == 4

    def test_list_pagination(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions?limit=2&offset=0", headers=AUTH)
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 4

    def test_list_filter_by_date(self, api_client, seed_data):
        resp = api_client.get(
            "/api/v1/transactions?date_from=2026-03-01&date_to=2026-03-31",
            headers=AUTH,
        )
        data = resp.json()
        assert data["total"] == 3  # txn001, txn002, txn003 (not txn004 in Feb)

    def test_list_filter_by_category(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions?category_id=rent", headers=AUTH)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["category_name"] == "Miete"

    def test_list_filter_recurring(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions?is_recurring=true", headers=AUTH)
        data = resp.json()
        assert all(t["is_recurring"] for t in data["items"])

    def test_list_search(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions?search=REWE", headers=AUTH)
        data = resp.json()
        assert data["total"] == 1

    def test_ibans_masked_by_default(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/txn001", headers=AUTH)
        data = resp.json()
        assert data["sender_iban"] == "DE00****0001"
        assert data["recipient_iban"] == "DE00****0002"

    def test_ibans_shown_when_requested(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/txn001?include_ibans=true", headers=AUTH)
        data = resp.json()
        assert data["sender_iban"] == "DE00000000000000000001"

    def test_get_nonexistent_returns_404(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/nope", headers=AUTH)
        assert resp.status_code == 404

    def test_includes_tags(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/txn001", headers=AUTH)
        data = resp.json()
        assert "important" in data["tags"]

    def test_includes_category_info(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/txn001", headers=AUTH)
        data = resp.json()
        assert data["category_id"] == "groceries"
        assert data["category_name"] == "Lebensmittel"
        assert data["category_type"] == "variabel"


# --- Categories ---


class TestCategoryEndpoints:
    def test_list_categories(self, api_client, seed_data):
        resp = api_client.get("/api/v1/categories", headers=AUTH)
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Lebensmittel" in names

    def test_get_category(self, api_client, seed_data):
        resp = api_client.get("/api/v1/categories/rent", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Miete"

    def test_create_category(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/categories",
            json={"id": "transport", "name": "Transport", "type": "variabel"},
            headers=AUTH,
        )
        assert resp.status_code == 201
        assert resp.json()["id"] == "transport"

    def test_create_duplicate_returns_409(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/categories",
            json={"id": "groceries", "name": "Duplicate", "type": "variabel"},
            headers=AUTH,
        )
        assert resp.status_code == 409

    def test_create_invalid_type_returns_400(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/categories",
            json={"id": "bad", "name": "Bad", "type": "invalid"},
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_update_category(self, api_client, seed_data):
        resp = api_client.patch(
            "/api/v1/categories/groceries",
            json={"name": "Essen & Trinken"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Essen & Trinken"

    def test_delete_category_with_transactions_returns_409(self, api_client, seed_data):
        resp = api_client.delete("/api/v1/categories/groceries", headers=AUTH)
        assert resp.status_code == 409

    def test_delete_unused_category(self, api_client, seed_data):
        resp = api_client.delete("/api/v1/categories/fun", headers=AUTH)
        assert resp.status_code == 204


# --- Tags ---


class TestTagEndpoints:
    def test_list_tags(self, api_client, seed_data):
        resp = api_client.get("/api/v1/tags", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_create_tag(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/tags",
            json={"id": "new-tag", "name": "Neuer Tag"},
            headers=AUTH,
        )
        assert resp.status_code == 201

    def test_delete_tag(self, api_client, seed_data):
        resp = api_client.delete("/api/v1/tags/review", headers=AUTH)
        assert resp.status_code == 204


# --- Transaction tag management ---


class TestTransactionTagManagement:
    def test_add_tag_to_transaction(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/transactions/txn002/tags",
            json={"tag_id": "important"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert "important" in resp.json()["tags"]

    def test_remove_tag_from_transaction(self, api_client, seed_data):
        resp = api_client.delete("/api/v1/transactions/txn001/tags/important", headers=AUTH)
        assert resp.status_code == 204

    def test_update_transaction_category(self, api_client, seed_data):
        resp = api_client.patch(
            "/api/v1/transactions/txn003/category",
            json={"category_id": "rent"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["category_id"] == "rent"

    def test_clear_transaction_category(self, api_client, seed_data):
        resp = api_client.patch(
            "/api/v1/transactions/txn001/category",
            json={"category_id": None},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["category_id"] is None


# --- Runs ---


class TestRunEndpoints:
    def test_list_runs(self, api_client, seed_data):
        resp = api_client.get("/api/v1/runs", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_run(self, api_client, seed_data):
        resp = api_client.get("/api/v1/runs/run001", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"

    def test_trigger_ingest_missing_file_returns_404(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/runs/ingest",
            json={"filename": "nonexistent.json"},
            headers=AUTH,
        )
        assert resp.status_code == 404

    def test_trigger_ingest_invalid_filename_returns_400(self, api_client, seed_data):
        resp = api_client.post(
            "/api/v1/runs/ingest",
            json={"filename": "../../../etc/passwd"},
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_trigger_ingest_requires_auth(self, api_client):
        resp = api_client.post(
            "/api/v1/runs/ingest",
            json={"filename": "test.json"},
        )
        assert resp.status_code == 401


# --- Aggregates ---


class TestAggregateEndpoints:
    def test_summary(self, api_client, seed_data):
        resp = api_client.get("/api/v1/aggregates/summary", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["total_income"]) == 3500.0
        assert (
            float(data["total_expenses"]) == -892.5
        )  # -42.50 + -850.00 (internal_transfer excluded)
        assert data["transaction_count"] == 3  # excludes internal transfer

    def test_summary_with_date_filter(self, api_client, seed_data):
        resp = api_client.get(
            "/api/v1/aggregates/summary?date_from=2026-03-10",
            headers=AUTH,
        )
        data = resp.json()
        assert data["transaction_count"] == 2  # txn001 (Mar 15) + txn003 (Mar 10)

    def test_by_category(self, api_client, seed_data):
        resp = api_client.get("/api/v1/aggregates/by-category", headers=AUTH)
        assert resp.status_code == 200
        categories = {c["category_id"]: c for c in resp.json()}
        assert "groceries" in categories
        assert "rent" in categories

    def test_monthly(self, api_client, seed_data):
        resp = api_client.get("/api/v1/aggregates/monthly", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        # Should be chronological
        months = [d["month"] for d in data]
        assert months == sorted(months)

    def test_recurring_patterns(self, api_client, seed_data):
        resp = api_client.get("/api/v1/aggregates/recurring-patterns", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["recipient"] == "Vermieter GmbH"


# --- Reports ---


class TestReportEndpoints:
    def test_list_reports(self, api_client, seed_data):
        resp = api_client.get("/api/v1/reports", headers=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_report(self, api_client, seed_data):
        resp = api_client.get("/api/v1/reports/rpt001", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["report_type"] == "weekly_analysis"
        assert resp.json()["content"]["summary"] == "Test report"

    def test_get_nonexistent_report(self, api_client, seed_data):
        resp = api_client.get("/api/v1/reports/nope", headers=AUTH)
        assert resp.status_code == 404
