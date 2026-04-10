"""Tests for the CSV export API endpoints."""

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def exports_dir(tmp_path):
    """Create a temp exports directory with sample CSV files."""
    (tmp_path / "umsaetze_2026-01-15.csv").write_text("header\nrow1")
    (tmp_path / "depot_positionen_2026-01-15.csv").write_text("header\nrow1")
    return tmp_path


@pytest.fixture
def client(exports_dir):
    """Create a test client with patched env vars."""
    with patch.dict(os.environ, {"EXPORTS_DIR": str(exports_dir), "API_TOKEN": "test-secret"}):
        # Re-import to pick up patched env
        import importlib
        import src.api.serve_exports as mod
        importlib.reload(mod)
        yield TestClient(mod.app)


@pytest.fixture
def client_no_auth(exports_dir):
    """Test client with no API_TOKEN set."""
    with patch.dict(os.environ, {"EXPORTS_DIR": str(exports_dir), "API_TOKEN": ""}):
        import importlib
        import src.api.serve_exports as mod
        importlib.reload(mod)
        yield TestClient(mod.app)


class TestHealth:
    def test_health_no_auth_needed(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAuth:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/exports")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, client):
        resp = client.get("/exports", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_valid_token(self, client):
        resp = client.get("/exports", headers={"Authorization": "Bearer test-secret"})
        assert resp.status_code == 200

    def test_no_auth_when_token_empty(self, client_no_auth):
        resp = client_no_auth.get("/exports")
        assert resp.status_code == 200


class TestListExports:
    def test_lists_csv_files(self, client):
        resp = client.get("/exports", headers={"Authorization": "Bearer test-secret"})
        data = resp.json()
        filenames = [f["filename"] for f in data["files"]]
        assert "umsaetze_2026-01-15.csv" in filenames
        assert "depot_positionen_2026-01-15.csv" in filenames

    def test_includes_file_metadata(self, client):
        resp = client.get("/exports", headers={"Authorization": "Bearer test-secret"})
        f = resp.json()["files"][0]
        assert "size_bytes" in f
        assert "modified" in f


class TestLatestExports:
    def test_groups_by_type(self, client):
        resp = client.get("/exports/latest", headers={"Authorization": "Bearer test-secret"})
        data = resp.json()["latest"]
        assert "umsaetze" in data
        assert "depot_positionen" in data


class TestDownload:
    def test_download_existing_file(self, client):
        resp = client.get(
            "/exports/umsaetze_2026-01-15.csv",
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_download_nonexistent_returns_404(self, client):
        resp = client.get(
            "/exports/nonexistent.csv",
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 404


class TestPathTraversal:
    def test_dotdot_rejected(self, client):
        resp = client.get(
            "/exports/../../../etc/passwd",
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code in (400, 404, 422)

    def test_special_chars_rejected(self, client):
        resp = client.get(
            "/exports/foo;bar.csv",
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 400


class TestSyncStart:
    def test_start_requires_auth(self, client):
        resp = client.post("/sync/start")
        assert resp.status_code == 401

    def test_start_forwards_to_worker(self, client):
        mock_response = httpx.Response(
            200,
            json={"status": "pending_tan", "session_id": "abc-123"},
        )
        with patch("src.api.serve_exports.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            resp = client.post(
                "/sync/start", headers={"Authorization": "Bearer test-secret"}
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "pending_tan"
            assert "session_id" in resp.json()

    def test_start_worker_unreachable_returns_503(self, client):
        with patch("src.api.serve_exports.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            resp = client.post(
                "/sync/start", headers={"Authorization": "Bearer test-secret"}
            )
            assert resp.status_code == 503
            assert "unreachable" in resp.json()["detail"].lower()


class TestSyncConfirm:
    def test_confirm_requires_auth(self, client):
        resp = client.post("/sync/confirm?session_id=abc")
        assert resp.status_code == 401

    def test_confirm_forwards_to_worker(self, client):
        mock_response = httpx.Response(
            200,
            json={"status": "done", "message": "Export completed"},
        )
        with patch("src.api.serve_exports.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            resp = client.post(
                "/sync/confirm?session_id=abc-123",
                headers={"Authorization": "Bearer test-secret"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "done"

    def test_confirm_worker_unreachable_returns_503(self, client):
        with patch("src.api.serve_exports.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            resp = client.post(
                "/sync/confirm?session_id=abc-123",
                headers={"Authorization": "Bearer test-secret"},
            )
            assert resp.status_code == 503
