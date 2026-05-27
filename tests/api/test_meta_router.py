"""Tests for app metadata endpoints."""

from __future__ import annotations

import os
from unittest.mock import patch

AUTH = {"Authorization": "Bearer test-secret"}


def test_version_requires_auth():
    with patch.dict(
        os.environ,
        {
            "API_TOKEN": "test-secret",
            "K_FIN_BACKEND_VERSION": "v9.8.7",
        },
    ):
        from src.api.app import create_app
        from fastapi.testclient import TestClient

        client = TestClient(create_app())

        resp = client.get("/api/v1/meta/version")

    assert resp.status_code == 401


def test_version_returns_deployed_backend_version():
    with patch.dict(
        os.environ,
        {
            "API_TOKEN": "test-secret",
            "K_FIN_BACKEND_VERSION": "v9.8.7",
        },
    ):
        from fastapi.testclient import TestClient

        from src.api.app import create_app

        client = TestClient(create_app())
        resp = client.get("/api/v1/meta/version", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"backend_version": "v9.8.7"}
