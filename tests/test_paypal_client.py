"""Connector unit tests for PayPalClient (M16-P2b).

Drives the OAuth2 client-credentials flow and the paginated reporting
fetch against in-process fakes — no network, dummy sandbox credentials.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.external.paypal_client import WINDOW_MAX_DAYS, PayPalClient


@pytest.fixture
def client():
    with patch("src.external.paypal_client.settings") as mock_settings:
        mock_settings.paypal_client_id = "test-client-id"
        mock_settings.paypal_client_secret = "test-client-secret"
        mock_settings.paypal_environment = "sandbox"
        yield PayPalClient()


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )


def _txn(txn_id: str) -> dict:
    return {"transaction_info": {"transaction_id": txn_id}}


def _patch_oauth(token: str = "test-access-token", status: int = 200):
    """Patch httpx.AsyncClient.post so the OAuth call returns *token*."""

    async def fake_post(self, url, *, data=None, headers=None):
        body = {"access_token": token, "expires_in": 32400} if status == 200 else {}
        return _FakeResponse(status, body)

    return patch.object(httpx.AsyncClient, "post", fake_post)


def _patch_pages(pages: list[dict], capture: list[dict] | None = None):
    """Patch httpx.AsyncClient.get to serve one body per requested page."""

    async def fake_get(self, url, *, params=None, headers=None):
        if capture is not None:
            capture.append(dict(params or {}))
        page = int((params or {}).get("page", 1))
        return _FakeResponse(200, pages[page - 1])

    return patch.object(httpx.AsyncClient, "get", fake_get)


# ---------------------------------------------------------------------------
# Environment selection
# ---------------------------------------------------------------------------


def test_base_url_sandbox_by_default(client):
    assert "sandbox" in client.base_url


def test_base_url_live_only_for_live_environment():
    with patch("src.external.paypal_client.settings") as s:
        s.paypal_client_id = s.paypal_client_secret = "x"
        s.paypal_environment = "live"
        assert PayPalClient().base_url == "https://api-m.paypal.com"


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_failure_raises_runtimeerror(client):
    with _patch_oauth(status=401):
        with pytest.raises(RuntimeError, match="OAuth"):
            await client._bearer_token()


@pytest.mark.asyncio
async def test_token_is_cached_between_calls(client):
    with _patch_oauth(token="cached-token"):
        first = await client._bearer_token()
        second = await client._bearer_token()
    assert first == second == "cached-token"


# ---------------------------------------------------------------------------
# Paginated fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_transactions_walks_every_page(client):
    pages = [
        {"transaction_details": [_txn("A"), _txn("B")], "total_pages": 2, "page": 1},
        {"transaction_details": [_txn("C")], "total_pages": 2, "page": 2},
    ]
    with _patch_oauth(), _patch_pages(pages):
        result = await client.get_transactions(date(2026, 5, 1), date(2026, 5, 20))
    ids = [r["transaction_info"]["transaction_id"] for r in result]
    assert ids == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_get_transactions_rejects_oversized_window(client):
    """A single window wider than the 31-day cap is rejected before any
    network call — the caller must chunk via get_transactions_range()."""
    with pytest.raises(ValueError, match="31-day"):
        await client.get_transactions(date(2026, 1, 1), date(2026, 3, 1))


@pytest.mark.asyncio
async def test_get_transactions_range_chunks_into_windows(client):
    """A 70-day range must be split into 3 sub-windows (≤31 days each)."""
    captured: list[dict] = []
    page = {"transaction_details": [_txn("X")], "total_pages": 1, "page": 1}
    with _patch_oauth(), _patch_pages([page], capture=captured):
        result = await client.get_transactions_range(
            date(2026, 1, 1), date(2026, 3, 12)
        )
    # 70 days / 31 → 3 windows → 3 page-1 requests → 3 transactions.
    assert len(result) == 3
    assert len(captured) == 3


@pytest.mark.asyncio
async def test_get_transactions_range_single_window_when_short(client):
    captured: list[dict] = []
    page = {"transaction_details": [_txn("Y")], "total_pages": 1, "page": 1}
    with _patch_oauth(), _patch_pages([page], capture=captured):
        result = await client.get_transactions_range(
            date(2026, 5, 1), date(2026, 5, 1) + timedelta(days=WINDOW_MAX_DAYS)
        )
    assert len(captured) == 1
    assert len(result) == 1
