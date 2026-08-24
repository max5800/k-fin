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
    SyncStage,
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

    # Some endpoints build their own engine from settings.database_url
    # (background tasks, agent orchestrator) — patch it too so create_app
    # picks up a valid URL instead of the default empty string.
    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
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
                source=SyncStage.NORMALIZE,
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
                    external_id=cid,
                    raw_data={"stub": True},
                )
            )

        s.flush()  # Ensure FKs are satisfied before dependents

        # Transactions
        s.add(
            NormalizedTransaction(
                id="txn001",
                raw_content_hash="a" * 64,
                external_id="CD001",
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
                external_id="CD002",
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
        assert data["items"][0]["category"]["name"] == "Miete"

    def test_list_filter_by_single_tag(self, api_client, seed_data, db_engine):
        # seed_data already wires txn001 -> important. Add txn002 -> review
        # so we can exercise OR-semantics across two distinct tags.
        with Session(db_engine) as s:
            s.add(TransactionTag(transaction_id="txn002", tag_id="review"))
            s.commit()

        resp = api_client.get("/api/v1/transactions?tag_ids=important", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        ids = {t["id"] for t in data["items"]}
        assert ids == {"txn001"}

    def test_list_filter_by_multiple_tags_or_semantics(
        self, api_client, seed_data, db_engine
    ):
        with Session(db_engine) as s:
            s.add(TransactionTag(transaction_id="txn002", tag_id="review"))
            s.commit()

        resp = api_client.get(
            "/api/v1/transactions?tag_ids=important&tag_ids=review",
            headers=AUTH,
        )
        assert resp.status_code == 200
        data = resp.json()
        # OR-semantics: both txns surface.
        assert data["total"] == 2
        ids = {t["id"] for t in data["items"]}
        assert ids == {"txn001", "txn002"}

    def test_list_filter_by_tag_no_match(self, api_client, seed_data):
        resp = api_client.get(
            "/api/v1/transactions?tag_ids=does-not-exist", headers=AUTH
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_filter_by_tag_no_row_duplication(
        self, api_client, seed_data, db_engine
    ):
        # A tx with multiple matching tags must not be counted twice — this
        # would happen with a naive JOIN instead of an EXISTS subquery.
        with Session(db_engine) as s:
            s.add(TransactionTag(transaction_id="txn001", tag_id="review"))
            s.commit()

        resp = api_client.get(
            "/api/v1/transactions?tag_ids=important&tag_ids=review",
            headers=AUTH,
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "txn001"

    def test_list_filter_recurring(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions?is_recurring=true", headers=AUTH)
        data = resp.json()
        assert all(t["is_recurring"] for t in data["items"])

    def test_list_search(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions?search=REWE", headers=AUTH)
        data = resp.json()
        assert data["total"] == 1

    def test_get_nonexistent_returns_404(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/nope", headers=AUTH)
        assert resp.status_code == 404

    def test_includes_tags(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/txn001", headers=AUTH)
        data = resp.json()
        assert any(t["id"] == "important" for t in data["tags"])

    def test_includes_category_info(self, api_client, seed_data):
        resp = api_client.get("/api/v1/transactions/txn001", headers=AUTH)
        data = resp.json()
        assert data["category"]["id"] == "groceries"
        assert data["category"]["name"] == "Lebensmittel"
        assert data["category"]["type"] == "variabel"


# --- Categories ---


class TestCategoryEndpoints:
    def test_list_categories(self, api_client, seed_data):
        resp = api_client.get("/api/v1/categories", headers=AUTH)
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Lebensmittel" in names

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

    def test_delete_unused_category(self, api_client, seed_data):
        resp = api_client.delete("/api/v1/categories/fun", headers=AUTH)
        assert resp.status_code == 204

    def test_delete_rejects_active_and_inactive_transaction_history(
        self, api_client, db_engine, seed_data
    ):
        with Session(db_engine) as s:
            s.add(
                Category(
                    id="audit-protected",
                    name="Audit Protected",
                    type=TypeEnum.VARIABEL,
                )
            )
            for marker in ("e", "f"):
                s.add(
                    RawTransaction(
                        content_hash=marker * 64,
                        external_id=f"TEST-{marker}",
                        raw_data={"stub": True},
                    )
                )
            s.flush()
            for tx_id, marker, active in (
                ("audit-active", "e", True),
                ("audit-inactive", "f", False),
            ):
                s.add(
                    NormalizedTransaction(
                        id=tx_id,
                        raw_content_hash=marker * 64,
                        booking_date=date(2026, 3, 20),
                        valuation_date=date(2026, 3, 20),
                        amount=Decimal("-1.00"),
                        currency="EUR",
                        category_id="audit-protected",
                        is_active=active,
                        normalization_status="active" if active else "superseded",
                        is_recurring=False,
                        is_outlier=False,
                        internal_transfer=False,
                        accounting_class="variable_discretionary_consumption",
                        accounting_confidence=Decimal("0.950"),
                        accounting_version=2,
                    )
                )
            s.commit()

        resp = api_client.delete("/api/v1/categories/audit-protected", headers=AUTH)
        assert resp.status_code == 409

        with Session(db_engine) as s:
            assert s.get(Category, "audit-protected") is not None
            for tx_id in ("audit-active", "audit-inactive"):
                tx = s.get(NormalizedTransaction, tx_id)
                assert tx.category_id == "audit-protected"
                assert tx.accounting_class == "variable_discretionary_consumption"
                assert tx.accounting_confidence == Decimal("0.950")
                assert tx.accounting_version == 2


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


# --- Runs ---


class TestRunEndpoints:
    def test_list_runs(self, api_client, seed_data):
        resp = api_client.get("/api/v1/runs", headers=AUTH)
        assert resp.status_code == 200
        # seed_data only creates a SyncRun; the runs endpoint lists AgentRuns.
        assert resp.json()["total"] == 0


# --- Reports ---


class TestReportEndpoints:
    def test_list_reports(self, api_client, seed_data):
        resp = api_client.get("/api/v1/reports", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_get_report(self, api_client, seed_data):
        resp = api_client.get("/api/v1/reports/rpt001", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["id"] == "rpt001"
        assert resp.json()["title"] == "weekly_analysis — 2026-W07"

    def test_get_nonexistent_report(self, api_client, seed_data):
        resp = api_client.get("/api/v1/reports/nope", headers=AUTH)
        assert resp.status_code == 404
