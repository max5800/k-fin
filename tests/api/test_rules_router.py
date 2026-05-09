"""Tests for the categorization-rules router (`/categories/rules`).

Covers CRUD happy + error paths (invalid regex, missing FK, 404 on
update/delete) and the auth guard.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import Category, TypeEnum

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
