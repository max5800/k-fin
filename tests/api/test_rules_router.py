"""Tests for the categorization-rules router (`/categories/rules`).

Covers:
* CRUD happy + error paths (invalid regex, missing FK, 404 on update/delete)
* Apply-all sync path (matched / unchanged / processed counts)
* Apply-all async path (>_APPLY_ALL_SYNC_LIMIT rows → 202 Accepted)
* Auth guard
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
    Rule,
    TypeEnum,
)

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def api_client(db_engine):
    """TestClient backed by a fresh Postgres."""
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
def categories_seed(db_engine):
    with Session(db_engine) as s:
        s.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        s.add(Category(id="rent", name="Miete", type=TypeEnum.FIX))
        s.commit()


def _seed_tx(
    s: Session,
    *,
    tx_id: str,
    description: str,
    recipient: str | None = None,
    sender: str | None = None,
    category_id: str | None = None,
    active: bool = True,
) -> None:
    s.add(
        RawTransaction(
            content_hash=tx_id.ljust(64, "0"),
            external_id=None,
            raw_data={"stub": True},
        )
    )
    s.flush()
    s.add(
        NormalizedTransaction(
            id=tx_id.ljust(64, "0"),
            raw_content_hash=tx_id.ljust(64, "0"),
            booking_date=date(2026, 3, 15),
            valuation_date=date(2026, 3, 15),
            amount=Decimal("-10.00"),
            currency="EUR",
            sender=sender,
            recipient=recipient,
            description=description,
            category_id=category_id,
            is_active=active,
            is_recurring=False,
            is_outlier=False,
            internal_transfer=False,
        )
    )


# ── Auth ──────────────────────────────────────────────────────────────


class TestRulesAuth:
    def test_list_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/categories/rules")
        assert resp.status_code == 401

    def test_create_requires_auth(self, api_client):
        resp = api_client.post(
            "/api/v1/categories/rules",
            json={"regex_pattern": "rewe", "target_category_id": "groceries"},
        )
        assert resp.status_code == 401

    def test_apply_all_requires_auth(self, api_client):
        resp = api_client.post("/api/v1/categories/rules/apply-all")
        assert resp.status_code == 401


# ── CRUD ──────────────────────────────────────────────────────────────


class TestRulesCRUD:
    def test_list_empty(self, api_client, categories_seed):
        resp = api_client.get("/api/v1/categories/rules", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_then_list_sorted_by_priority_desc(
        self, api_client, categories_seed
    ):
        api_client.post(
            "/api/v1/categories/rules",
            json={
                "regex_pattern": "rewe",
                "target_category_id": "groceries",
                "priority": 5,
            },
            headers=AUTH,
        )
        api_client.post(
            "/api/v1/categories/rules",
            json={
                "regex_pattern": "vermieter",
                "target_category_id": "rent",
                "priority": 10,
            },
            headers=AUTH,
        )

        resp = api_client.get("/api/v1/categories/rules", headers=AUTH)
        assert resp.status_code == 200
        items = resp.json()
        assert [r["regex_pattern"] for r in items] == ["vermieter", "rewe"]
        assert items[0]["priority"] == 10
        # Schema contract (mirrors the UI's CategoryRule type)
        assert set(items[0].keys()) == {
            "id",
            "regex_pattern",
            "target_category_id",
            "priority",
        }

    def test_create_invalid_regex_returns_400(self, api_client, categories_seed):
        resp = api_client.post(
            "/api/v1/categories/rules",
            json={
                "regex_pattern": "(unclosed",
                "target_category_id": "groceries",
            },
            headers=AUTH,
        )
        assert resp.status_code == 400
        assert "regex" in resp.json()["detail"].lower()

    def test_create_unknown_category_returns_422(self, api_client, categories_seed):
        resp = api_client.post(
            "/api/v1/categories/rules",
            json={
                "regex_pattern": "rewe",
                "target_category_id": "does-not-exist",
            },
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_patch_updates_fields(self, api_client, categories_seed):
        created = api_client.post(
            "/api/v1/categories/rules",
            json={
                "regex_pattern": "rewe",
                "target_category_id": "groceries",
                "priority": 1,
            },
            headers=AUTH,
        ).json()
        rule_id = created["id"]

        resp = api_client.patch(
            f"/api/v1/categories/rules/{rule_id}",
            json={"priority": 99, "regex_pattern": "rewe|edeka"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["priority"] == 99
        assert body["regex_pattern"] == "rewe|edeka"
        assert body["target_category_id"] == "groceries"  # unchanged

    def test_patch_invalid_regex_returns_400(self, api_client, categories_seed):
        created = api_client.post(
            "/api/v1/categories/rules",
            json={"regex_pattern": "rewe", "target_category_id": "groceries"},
            headers=AUTH,
        ).json()
        resp = api_client.patch(
            f"/api/v1/categories/rules/{created['id']}",
            json={"regex_pattern": "(broken"},
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_patch_404_for_missing_rule(self, api_client, categories_seed):
        resp = api_client.patch(
            "/api/v1/categories/rules/9999",
            json={"priority": 1},
            headers=AUTH,
        )
        assert resp.status_code == 404

    def test_delete_existing(self, api_client, categories_seed):
        created = api_client.post(
            "/api/v1/categories/rules",
            json={"regex_pattern": "rewe", "target_category_id": "groceries"},
            headers=AUTH,
        ).json()
        resp = api_client.delete(
            f"/api/v1/categories/rules/{created['id']}", headers=AUTH
        )
        assert resp.status_code == 204
        # Gone afterwards
        listing = api_client.get("/api/v1/categories/rules", headers=AUTH).json()
        assert listing == []

    def test_delete_404(self, api_client, categories_seed):
        resp = api_client.delete("/api/v1/categories/rules/9999", headers=AUTH)
        assert resp.status_code == 404


# ── Apply-all ─────────────────────────────────────────────────────────


class TestRulesApplyAll:
    def test_apply_all_no_rules_no_tx(self, api_client, categories_seed):
        resp = api_client.post(
            "/api/v1/categories/rules/apply-all", headers=AUTH
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "completed",
            "processed": 0,
            "matched": 0,
            "unchanged": 0,
        }

    def test_apply_all_matches_uncategorised_only(
        self, api_client, categories_seed, db_engine
    ):
        with Session(db_engine) as s:
            s.add(
                Rule(
                    regex_pattern="rewe", target_category_id="groceries", priority=10
                )
            )
            _seed_tx(s, tx_id="a", description="Einkauf REWE", recipient="REWE")
            _seed_tx(s, tx_id="b", description="REWE Berlin", recipient="REWE")
            # Already-categorised — must NOT be re-touched
            _seed_tx(
                s,
                tx_id="c",
                description="REWE other",
                recipient="REWE",
                category_id="rent",
            )
            # No match
            _seed_tx(s, tx_id="d", description="Aldi", recipient="ALDI")
            # Superseded audit row — must not be scanned or changed.
            _seed_tx(
                s,
                tx_id="e",
                description="REWE stale",
                recipient="REWE",
                active=False,
            )
            s.commit()

        resp = api_client.post(
            "/api/v1/categories/rules/apply-all", headers=AUTH
        )
        assert resp.status_code == 200
        body = resp.json()
        # 3 uncategorised processed (a, b, d); 2 matched (a, b); d unchanged.
        assert body["processed"] == 3
        assert body["matched"] == 2
        assert body["unchanged"] == 1

        # Verify DB side-effects
        with Session(db_engine) as s:
            tx_a = s.get(NormalizedTransaction, "a".ljust(64, "0"))
            tx_c = s.get(NormalizedTransaction, "c".ljust(64, "0"))
            tx_d = s.get(NormalizedTransaction, "d".ljust(64, "0"))
            tx_e = s.get(NormalizedTransaction, "e".ljust(64, "0"))
            assert tx_a.category_id == "groceries"
            assert tx_a.accounting_class == "reconciled_consumption"
            assert tx_a.accounting_version == 2
            assert tx_c.category_id == "rent"  # untouched
            assert tx_d.category_id is None
            assert tx_e.category_id is None

    def test_apply_all_priority_wins(self, api_client, categories_seed, db_engine):
        with Session(db_engine) as s:
            # Two rules can both match "rewe edeka" — higher priority wins.
            s.add(Rule(regex_pattern="rewe", target_category_id="rent", priority=1))
            s.add(
                Rule(
                    regex_pattern="rewe",
                    target_category_id="groceries",
                    priority=10,
                )
            )
            _seed_tx(s, tx_id="a", description="REWE x", recipient="rewe")
            s.commit()

        api_client.post("/api/v1/categories/rules/apply-all", headers=AUTH)

        with Session(db_engine) as s:
            tx = s.get(NormalizedTransaction, "a".ljust(64, "0"))
            assert tx.category_id == "groceries"

    def test_apply_all_async_returns_202(
        self, api_client, categories_seed, db_engine
    ):
        # Force the threshold low for the test by patching the constant.
        from src.api.routers import rules as rules_router

        with Session(db_engine) as s:
            s.add(
                Rule(
                    regex_pattern="rewe", target_category_id="groceries", priority=1
                )
            )
            for i in range(3):
                _seed_tx(s, tx_id=f"x{i}", description="rewe", recipient="rewe")
            s.commit()

        with patch.object(rules_router, "_APPLY_ALL_SYNC_LIMIT", 2):
            resp = api_client.post(
                "/api/v1/categories/rules/apply-all", headers=AUTH
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["processed"] == 3
        assert body["matched"] is None
        assert body["unchanged"] is None
