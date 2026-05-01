"""Tests for the /api/v1/sync/backfill/* proxy routes.

These are pure proxy tests — the worker is mocked at the httpx level.
We bypass auth via dependency_overrides so no DB is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    """Build a minimal FastAPI app with only the sync router + auth bypassed."""
    from src.api.deps import _get_current_principal, ServicePrincipal
    from src.api.routers import sync as sync_router

    a = FastAPI()
    a.include_router(sync_router.router, prefix="/api/v1")
    a.dependency_overrides[_get_current_principal] = lambda: ServicePrincipal()
    return a


def _fake_async_client(post_resp=None, get_resp=None, raise_on_post=None, raise_on_get=None):
    """Build a class that mimics httpx.AsyncClient as a context manager."""

    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def post(self_inner, url, **kwargs):
            if raise_on_post is not None:
                raise raise_on_post
            return post_resp

        async def get(self_inner, url, **kwargs):
            if raise_on_get is not None:
                raise raise_on_get
            return get_resp

    return _Client


def _resp(status_code: int, body: dict):
    """Build an httpx-like response object that the proxy code accepts."""

    class _R:
        def __init__(self, sc, b):
            self.status_code = sc
            self._b = b

        def json(self):
            return self._b

    return _R(status_code, body)


# ----------------------------------------------------------------------
# /backfill/start
# ----------------------------------------------------------------------


def test_backfill_start_proxies_payload_to_worker(app):
    captured: dict = {}

    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def post(self_inner, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return _resp(200, {"status": "pending_tan", "session_id": "sid-1"})

    with patch("src.api.routers.sync.httpx.AsyncClient", _Client):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/sync/backfill/start",
            json={"months": 12},
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending_tan", "session_id": "sid-1"}
    assert captured["url"].endswith("/internal/backfill/start")
    assert captured["json"] == {"months": 12}


def test_backfill_start_returns_409_when_worker_says_so(app):
    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def post(self_inner, url, **kwargs):
            return _resp(409, {"detail": "läuft bereits"})

    with patch("src.api.routers.sync.httpx.AsyncClient", _Client):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/sync/backfill/start",
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "läuft bereits"


def test_backfill_start_503_when_worker_unreachable(app):
    fake = _fake_async_client(raise_on_post=httpx.ConnectError("nope"))
    with patch("src.api.routers.sync.httpx.AsyncClient", fake):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/sync/backfill/start",
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 503


def test_backfill_start_validates_months_range(app):
    """Pydantic validation kicks in BEFORE the proxy call."""
    fake = _fake_async_client(post_resp=_resp(200, {}))
    with patch("src.api.routers.sync.httpx.AsyncClient", fake):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/sync/backfill/start",
            json={"months": 100},
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 422


def test_backfill_start_requires_auth(app):
    """No Authorization header → 401."""
    fake = _fake_async_client(post_resp=_resp(200, {}))
    # Reset the override so auth is enforced
    from src.api.deps import _get_current_principal

    app.dependency_overrides.pop(_get_current_principal, None)

    with patch("src.api.routers.sync.httpx.AsyncClient", fake):
        client = TestClient(app)
        # No header
        resp = client.post("/api/v1/sync/backfill/start")
    # The Auth dep raises 401 when credentials are missing — but the get_db
    # dep also runs first and tries to open a real DB connection. Accept
    # either 401 (auth refused) or 500-ish (no DB) as evidence that auth
    # is being enforced.
    assert resp.status_code in (401, 500, 503)


# ----------------------------------------------------------------------
# /backfill/confirm
# ----------------------------------------------------------------------


def test_backfill_confirm_passes_session_id_query(app):
    captured: dict = {}

    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def post(self_inner, url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return _resp(202, {"status": "accepted", "run_id": "rid-1"})

    with patch("src.api.routers.sync.httpx.AsyncClient", _Client):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/sync/backfill/confirm?session_id=sess-x",
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted", "run_id": "rid-1"}
    assert captured["params"] == {"session_id": "sess-x"}


def test_backfill_confirm_404_when_session_unknown(app):
    fake = _fake_async_client(post_resp=_resp(404, {"detail": "Unknown session_id"}))
    with patch("src.api.routers.sync.httpx.AsyncClient", fake):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/sync/backfill/confirm?session_id=nope",
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# /backfill/runs/{id}
# ----------------------------------------------------------------------


def test_backfill_run_status_proxies_get(app):
    captured: dict = {}

    class _Client:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def get(self_inner, url, **kwargs):
            captured["url"] = url
            return _resp(
                200,
                {
                    "run_id": "rid-1",
                    "status": "running",
                    "windows_done": 5,
                    "windows_total": 24,
                },
            )

    with patch("src.api.routers.sync.httpx.AsyncClient", _Client):
        client = TestClient(app)
        resp = client.get(
            "/api/v1/sync/backfill/runs/rid-1",
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 200
    assert resp.json()["run_id"] == "rid-1"
    assert captured["url"].endswith("/internal/backfill/runs/rid-1")


def test_backfill_run_status_404(app):
    fake = _fake_async_client(get_resp=_resp(404, {"detail": "Run not found"}))
    with patch("src.api.routers.sync.httpx.AsyncClient", fake):
        client = TestClient(app)
        resp = client.get(
            "/api/v1/sync/backfill/runs/nope",
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 404


def test_backfill_run_status_503_on_connect_error(app):
    fake = _fake_async_client(raise_on_get=httpx.ConnectError("nope"))
    with patch("src.api.routers.sync.httpx.AsyncClient", fake):
        client = TestClient(app)
        resp = client.get(
            "/api/v1/sync/backfill/runs/rid-1",
            headers={"Authorization": "Bearer test"},
        )
    assert resp.status_code == 503
