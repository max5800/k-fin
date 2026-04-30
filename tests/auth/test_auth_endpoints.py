"""Integration tests for /auth/* endpoints (require Postgres via testcontainers)."""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.api.auth.passwords import hash_password
from src.core.db import get_db
from src.core.db.models import User

AUTH_ENV = {
    "JWT_SECRET": "integration-test-secret-minimum-32-chars!!",
    "API_TOKEN": "service-token-for-mcp",
    "APP_ENV": "development",
    "BOOTSTRAP_LOGIN_ENABLED": "false",
}


def _make_user(
    db: Session, email: str = "max@example.com", password: str = "hunter2hunter2"
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name="Max",
        password_hash=hash_password(password),
        is_active=True,
        role="admin",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_client(db_engine, db_url: str) -> TestClient:
    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    with patch.dict(os.environ, {**AUTH_ENV, "DATABASE_URL": db_url}, clear=False):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)


@pytest.fixture
def client(db_engine):
    db_url = db_engine.url.render_as_string(hide_password=False)
    return _make_client(db_engine, db_url)


@pytest.fixture
def seeded(db_engine, db_session):
    db_url = db_engine.url.render_as_string(hide_password=False)
    user = _make_user(db_session)
    c = _make_client(db_engine, db_url)
    return c, user


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_returns_jwt_and_user(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    resp = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "hunter2hunter2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20
    assert body["user"]["email"] == "max@example.com"
    assert body["user"]["display_name"] == "Max"
    assert "password_hash" not in body["user"]


def test_login_wrong_password_returns_401(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    resp = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401


def test_login_unknown_email_returns_401(db_engine):
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    resp = c.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------


def test_me_with_valid_jwt(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    token = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "hunter2hunter2"},
    ).json()["access_token"]

    resp = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Max"


def test_me_without_token_returns_401(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_me_with_service_token_returns_401(client):
    """Service (API_TOKEN) cannot reach /auth/me — it's JWT-only."""
    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer service-token-for-mcp"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Protected routes still work with service token
# ---------------------------------------------------------------------------


def test_service_token_reaches_protected_route(client):
    resp = client.get(
        "/api/v1/transactions",
        headers={"Authorization": "Bearer service-token-for-mcp"},
    )
    # 200 or empty list — auth passed, DB may be empty
    assert resp.status_code in (200, 422, 404)


def test_protected_route_rejects_no_token(client):
    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


def test_change_password(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    token = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "hunter2hunter2"},
    ).json()["access_token"]

    resp = c.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "hunter2hunter2",
            "new_password": "newpassword123456",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    # Old password no longer works
    assert (
        c.post(
            "/api/v1/auth/login",
            json={"email": "max@example.com", "password": "hunter2hunter2"},
        ).status_code
        == 401
    )

    # New password works
    assert (
        c.post(
            "/api/v1/auth/login",
            json={"email": "max@example.com", "password": "newpassword123456"},
        ).status_code
        == 200
    )


def test_change_password_too_short(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    token = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "hunter2hunter2"},
    ).json()["access_token"]

    resp = c.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2hunter2", "new_password": "short"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


def test_change_password_wrong_current(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    token = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "hunter2hunter2"},
    ).json()["access_token"]

    resp = c.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrongcurrent", "new_password": "newpassword123456"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_returns_204(db_engine, db_session):
    _make_user(db_session)
    c = _make_client(db_engine, db_engine.url.render_as_string(hide_password=False))

    token = c.post(
        "/api/v1/auth/login",
        json={"email": "max@example.com", "password": "hunter2hunter2"},
    ).json()["access_token"]

    resp = c.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204
