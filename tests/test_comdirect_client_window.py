"""Tests for ComdirectClient.get_transactions_window — the backfill primitive."""

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.external.comdirect_client import ComdirectClient


@pytest.fixture
def client():
    with patch("src.external.comdirect_client.settings") as mock_settings:
        mock_settings.comdirect_client_id = "test-id"
        mock_settings.comdirect_client_secret = "test-secret"
        mock_settings.comdirect_username = "john.doe"
        mock_settings.comdirect_pin = "12345"
        mock_settings.comdirect_tan_method = "pushTAN"
        inst = ComdirectClient()
        inst._secondary_token = "secondary-token"
        inst.access_token = "secondary-token"
        yield inst


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )


def _patch_httpx(response: _FakeResponse, capture: dict | None = None):
    """Patch httpx.AsyncClient so .get() returns the prepared response.

    If `capture` is given, the params kwarg is recorded into it.
    """

    async def fake_get(self, url, *, headers=None, params=None):
        if capture is not None:
            capture["url"] = url
            capture["params"] = params
            capture["headers"] = headers
        return response

    return patch.object(httpx.AsyncClient, "get", fake_get)


@pytest.mark.asyncio
async def test_window_happy_path_returns_values_and_matches(client):
    body = {
        "paging": {"index": 0, "matches": 3},
        "values": [
            {"transactionId": "t1", "bookingDate": "2025-06-15"},
            {"transactionId": "t2", "bookingDate": "2025-06-20"},
            {"transactionId": "t3", "bookingDate": "2025-06-28"},
        ],
    }
    captured: dict = {}

    with _patch_httpx(_FakeResponse(200, body), captured):
        values, matches = await client.get_transactions_window(
            "ACC-1", date(2025, 6, 1), date(2025, 6, 30)
        )

    assert matches == 3
    assert [v["transactionId"] for v in values] == ["t1", "t2", "t3"]
    # Both date params + state are sent in YYYY-MM-DD form
    assert captured["params"]["min-bookingDate"] == "2025-06-01"
    assert captured["params"]["max-bookingDate"] == "2025-06-30"
    assert captured["params"]["transactionState"] == "BOOKED"
    assert captured["params"]["paging-count"] == 500


@pytest.mark.asyncio
async def test_window_422_returns_empty_no_raise(client):
    """HTTP 422 (e.g. min-bookingDate beyond API lookback) is a soft stop."""
    with _patch_httpx(_FakeResponse(422, {"errorCode": "VALIDATION"}, text="too far back")):
        values, matches = await client.get_transactions_window(
            "ACC-1", date(2020, 1, 1), date(2020, 1, 31)
        )

    assert values == []
    assert matches == 0


@pytest.mark.asyncio
async def test_window_500_still_raises(client):
    with _patch_httpx(_FakeResponse(500, text="server error")):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_transactions_window(
                "ACC-1", date(2025, 6, 1), date(2025, 6, 30)
            )


@pytest.mark.asyncio
async def test_window_cap_hit_warns_but_returns(client, caplog):
    """When values count == paging_count, caller should split — but we still return data."""
    body = {
        "paging": {"index": 0, "matches": 750},
        "values": [{"transactionId": f"t{i}", "bookingDate": "2025-06-15"} for i in range(500)],
    }
    with _patch_httpx(_FakeResponse(200, body)):
        with caplog.at_level("WARNING"):
            values, matches = await client.get_transactions_window(
                "ACC-1", date(2025, 6, 1), date(2025, 6, 30), paging_count=500
            )

    assert len(values) == 500
    assert matches == 750
    assert any("paging cap" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_window_invalid_date_range_raises(client):
    with pytest.raises(ValueError, match="must be <="):
        await client.get_transactions_window(
            "ACC-1", date(2025, 7, 1), date(2025, 6, 1)
        )


@pytest.mark.asyncio
async def test_window_unauthenticated_raises():
    inst = ComdirectClient()
    # both tokens unset
    inst._secondary_token = None
    inst.access_token = None
    with pytest.raises(RuntimeError, match="Not authenticated"):
        await inst.get_transactions_window(
            "ACC-1", date(2025, 6, 1), date(2025, 6, 30)
        )


@pytest.mark.asyncio
async def test_window_custom_transaction_state(client):
    body = {"paging": {"matches": 0}, "values": []}
    captured: dict = {}
    with _patch_httpx(_FakeResponse(200, body), captured):
        await client.get_transactions_window(
            "ACC-1",
            date(2025, 6, 1),
            date(2025, 6, 30),
            transaction_state="BOTH",
        )
    assert captured["params"]["transactionState"] == "BOTH"


@pytest.mark.asyncio
async def test_window_matches_falls_back_to_count_if_paging_missing(client):
    """If the API response omits the paging block, matches == len(values)."""
    body = {
        "values": [
            {"transactionId": "t1", "bookingDate": "2025-06-15"},
            {"transactionId": "t2", "bookingDate": "2025-06-20"},
        ],
    }
    with _patch_httpx(_FakeResponse(200, body)):
        values, matches = await client.get_transactions_window(
            "ACC-1", date(2025, 6, 1), date(2025, 6, 30)
        )
    assert matches == 2
    assert len(values) == 2
