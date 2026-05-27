"""Tests for app metadata endpoints."""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.db import get_db

AUTH = {"Authorization": "Bearer test-secret"}


def _client(monkeypatch):
    from src.api import deps

    monkeypatch.setattr(deps.settings, "api_token", "test-secret")
    monkeypatch.setattr(deps.settings, "jwt_secret", "")

    app = create_app()
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def test_version_requires_auth(monkeypatch):
    with patch.dict(
        os.environ,
        {
            "API_TOKEN": "test-secret",
            "K_FIN_BACKEND_VERSION": "v9.8.7",
        },
    ):
        client = _client(monkeypatch)
        resp = client.get("/api/v1/meta/version")

    assert resp.status_code == 401


def test_version_returns_deployed_backend_version(monkeypatch):
    with patch.dict(
        os.environ,
        {
            "API_TOKEN": "test-secret",
            "K_FIN_BACKEND_VERSION": "v9.8.7",
        },
    ):
        client = _client(monkeypatch)
        resp = client.get("/api/v1/meta/version", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"backend_version": "v9.8.7"}
