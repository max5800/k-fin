"""Tests for the bootstrap login endpoint (DEV ONLY).

These tests do not need a database — they exercise the auth router in
isolation. The protected-endpoint smoke check is covered by the
existing finance-API integration tests.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _build_client(env: dict[str, str]) -> TestClient:
    with patch.dict(os.environ, env, clear=False):
        from src.api.app import create_app

        return TestClient(create_app())


BOOTSTRAP_ENV = {
    "APP_ENV": "development",
    "API_TOKEN": "test-api-token",
    "BOOTSTRAP_LOGIN_ENABLED": "true",
    "BOOTSTRAP_EMAIL": "dev@k-fin.local",
    "BOOTSTRAP_PASSWORD": "dev-secret",
}


@pytest.fixture
def bootstrap_client() -> TestClient:
    return _build_client(BOOTSTRAP_ENV)


def test_login_happy_path_returns_api_token(bootstrap_client):
    resp = bootstrap_client.post(
        "/api/v1/auth/login",
        json={"email": "dev@k-fin.local", "password": "dev-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"access_token": "test-api-token", "token_type": "bearer"}


def test_login_email_is_case_insensitive(bootstrap_client):
    resp = bootstrap_client.post(
        "/api/v1/auth/login",
        json={"email": "DEV@K-FIN.LOCAL", "password": "dev-secret"},
    )
    assert resp.status_code == 200


def test_login_wrong_password_returns_401(bootstrap_client):
    resp = bootstrap_client.post(
        "/api/v1/auth/login",
        json={"email": "dev@k-fin.local", "password": "wrong"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_login_wrong_email_returns_401_with_same_shape(bootstrap_client):
    resp = bootstrap_client.post(
        "/api/v1/auth/login",
        json={"email": "intruder@example.com", "password": "dev-secret"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_login_disabled_by_default_returns_404():
    client = _build_client(
        {
            "APP_ENV": "development",
            "API_TOKEN": "test-api-token",
            "BOOTSTRAP_LOGIN_ENABLED": "false",
            "BOOTSTRAP_EMAIL": "dev@k-fin.local",
            "BOOTSTRAP_PASSWORD": "dev-secret",
        }
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "dev@k-fin.local", "password": "dev-secret"},
    )
    assert resp.status_code == 404


def test_login_blocked_in_production_even_when_enabled():
    client = _build_client(
        {
            "APP_ENV": "production",
            "API_TOKEN": "test-api-token",
            "BOOTSTRAP_LOGIN_ENABLED": "true",
            "BOOTSTRAP_EMAIL": "dev@k-fin.local",
            "BOOTSTRAP_PASSWORD": "dev-secret",
        }
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "dev@k-fin.local", "password": "dev-secret"},
    )
    assert resp.status_code == 404


def test_login_misconfigured_returns_500():
    client = _build_client(
        {
            "APP_ENV": "development",
            "API_TOKEN": "test-api-token",
            "BOOTSTRAP_LOGIN_ENABLED": "true",
            "BOOTSTRAP_EMAIL": "",
            "BOOTSTRAP_PASSWORD": "",
        }
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "dev@k-fin.local", "password": "dev-secret"},
    )
    assert resp.status_code == 500
