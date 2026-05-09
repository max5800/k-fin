"""Tests for the app-settings router (/api/v1/settings).

Settings is a single-row table that stores user-tunable knobs the UI
can edit at runtime. The PUT body lets a caller update one knob at a
time — fields default to None and are only applied when explicitly set.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import AppSettings

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


class TestReadSettings:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/api/v1/settings")
        assert resp.status_code == 401

    def test_returns_defaults_when_row_missing(self, api_client, db_engine):
        resp = api_client.get("/api/v1/settings", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 25
        assert body["auto_apply_confidence"] == pytest.approx(0.60)

        # The GET should have lazily created the singleton row.
        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row is not None
            assert row.page_size == 25

    def test_returns_persisted_row(self, api_client, db_engine):
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.80"),
                    page_size=100,
                )
            )
            s.commit()

        resp = api_client.get("/api/v1/settings", headers=AUTH)
        body = resp.json()
        assert body["page_size"] == 100
        assert body["auto_apply_confidence"] == pytest.approx(0.80)


class TestUpdatePageSize:
    def test_requires_auth(self, api_client):
        resp = api_client.put("/api/v1/settings", json={"page_size": 50})
        assert resp.status_code == 401

    def test_updates_page_size(self, api_client, db_engine):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 50},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 50

        with Session(db_engine) as s:
            row = s.get(AppSettings, 1)
            assert row.page_size == 50

    def test_update_only_page_size_keeps_confidence(self, api_client, db_engine):
        # Pre-populate with a non-default confidence value.
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.80"),
                    page_size=25,
                )
            )
            s.commit()

        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 200},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 200
        # Untouched.
        assert body["auto_apply_confidence"] == pytest.approx(0.80)

    def test_below_minimum_returns_422(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 9},
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_at_minimum_succeeds(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 10},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 10

    def test_at_maximum_succeeds(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 200},
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 200

    def test_above_maximum_returns_422(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 201},
            headers=AUTH,
        )
        assert resp.status_code == 422

    def test_negative_returns_422(self, api_client):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": -1},
            headers=AUTH,
        )
        assert resp.status_code == 422


class TestUpdateBoth:
    def test_can_update_both_fields_in_one_call(self, api_client, db_engine):
        resp = api_client.put(
            "/api/v1/settings",
            json={"page_size": 100, "auto_apply_confidence": 0.75},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 100
        assert body["auto_apply_confidence"] == pytest.approx(0.75)

    def test_empty_body_is_a_noop_returning_current(self, api_client, db_engine):
        with Session(db_engine) as s:
            s.add(
                AppSettings(
                    id=1,
                    auto_apply_confidence=Decimal("0.42"),
                    page_size=42,
                )
            )
            s.commit()

        resp = api_client.put("/api/v1/settings", json={}, headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_size"] == 42
        assert body["auto_apply_confidence"] == pytest.approx(0.42)
