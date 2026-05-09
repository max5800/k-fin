"""Tests for ComdirectClient.get_depot_transactions paging loop.

The Swagger spec for ``/brokerage/v3/depots/{depotId}/transactions``
does not document ``paging-first``/``paging-count``, but the endpoint
accepts them in practice (mirroring the Banking transactions endpoint).
These tests pin the loop semantics:

- multi-page fetch terminates when ``paging.matches`` is reached
- short final page (``len < limit``) ends the loop
- HTTP 422 on a follow-up page is a soft stop (returns what we have)
- single-request payloads still work (back-compat)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.connector.comdirect_client import ComdirectClient


@pytest.fixture
def client():
    with patch("src.connector.comdirect_client.settings") as mock_settings:
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


def _patch_httpx_sequence(responses: list[_FakeResponse], capture: list | None = None):
    """Patch httpx.AsyncClient.get to return the prepared responses in order.

    Each call pops the next response. If ``capture`` is given, the params
    of every call are appended to it.
    """
    iterator = iter(responses)

    async def fake_get(self, url, *, headers=None, params=None):
        if capture is not None:
            capture.append({"url": url, "params": dict(params or {})})
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError(
                f"Unexpected extra HTTP call to {url} with params={params}"
            ) from exc

    return patch.object(httpx.AsyncClient, "get", fake_get)


def _tx(idx: int, isin: str = "DE0001234567"):
    return {
        "transactionId": f"dtx-{idx}",
        "bookingDate": "2025-06-15",
        "isin": isin,
        "transactionDirection": "BUY",
    }


@pytest.mark.asyncio
async def test_paging_loop_collects_all_pages(client):
    """Two-page response: loop fetches both pages and returns the union."""
    page1 = {
        "paging": {"index": 0, "matches": 7},
        "values": [_tx(i) for i in range(5)],
    }
    page2 = {
        "paging": {"index": 5, "matches": 7},
        "values": [_tx(i) for i in range(5, 7)],
    }
    captured: list = []

    with _patch_httpx_sequence([_FakeResponse(200, page1), _FakeResponse(200, page2)], captured):
        result = await client.get_depot_transactions("DEPOT-1", limit=5)

    assert len(result) == 7
    assert [r["transactionId"] for r in result] == [f"dtx-{i}" for i in range(7)]

    # Two requests sent, with paging-first incrementing by limit.
    assert len(captured) == 2
    assert captured[0]["params"]["paging-first"] == 0
    assert captured[0]["params"]["paging-count"] == 5
    assert captured[1]["params"]["paging-first"] == 5
    assert captured[1]["params"]["paging-count"] == 5


@pytest.mark.asyncio
async def test_paging_loop_stops_on_short_final_page(client):
    """Final page returns fewer rows than `limit` — loop terminates."""
    page1 = {
        "paging": {"index": 0, "matches": 8},
        "values": [_tx(i) for i in range(5)],
    }
    page2 = {
        # Server reports inflated matches but only delivers 2 rows.
        # Short page must terminate the loop regardless.
        "paging": {"index": 5, "matches": 8},
        "values": [_tx(i) for i in range(5, 7)],
    }
    captured: list = []

    with _patch_httpx_sequence([_FakeResponse(200, page1), _FakeResponse(200, page2)], captured):
        result = await client.get_depot_transactions("DEPOT-1", limit=5)

    assert len(result) == 7
    assert len(captured) == 2  # No third request


@pytest.mark.asyncio
async def test_paging_loop_single_page_no_extra_request(client):
    """If the first page already covers `matches`, no second request fires."""
    only_page = {
        "paging": {"index": 0, "matches": 3},
        "values": [_tx(i) for i in range(3)],
    }
    captured: list = []

    with _patch_httpx_sequence([_FakeResponse(200, only_page)], captured):
        result = await client.get_depot_transactions("DEPOT-1", limit=100)

    assert len(result) == 3
    assert len(captured) == 1
    assert captured[0]["params"]["paging-first"] == 0


@pytest.mark.asyncio
async def test_paging_loop_422_on_followup_is_soft_stop(client, caplog):
    """If the server rejects paging-first > 0 with 422, return what we have.

    Mirrors the documented account-tx behaviour where offset paging is
    not supported. The first page must still be delivered.
    """
    page1 = {
        # matches says there's more, but the server will refuse the next page.
        "paging": {"index": 0, "matches": 50},
        "values": [_tx(i) for i in range(5)],
    }
    err422 = _FakeResponse(422, {"errorCode": "VALIDATION"}, text="paging-first not supported")

    with _patch_httpx_sequence([_FakeResponse(200, page1), err422]):
        with caplog.at_level("WARNING"):
            result = await client.get_depot_transactions("DEPOT-1", limit=5)

    assert len(result) == 5
    assert any("refuses offset paging" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_paging_loop_max_pages_safety_cap(client, caplog):
    """If the server keeps reporting more `matches` than it delivers,
    the safety cap stops us from looping forever."""
    # Each page returns exactly `limit` rows and a matches that always
    # claims one more than we have — without max_pages this would loop.
    pages = []
    for page_idx in range(5):
        pages.append(
            _FakeResponse(
                200,
                {
                    "paging": {"index": page_idx * 2, "matches": 999_999},
                    "values": [_tx(page_idx * 2), _tx(page_idx * 2 + 1)],
                },
            )
        )

    with _patch_httpx_sequence(pages):
        with caplog.at_level("WARNING"):
            result = await client.get_depot_transactions(
                "DEPOT-1", limit=2, max_pages=5
            )

    assert len(result) == 10  # 5 pages * 2 rows
    assert any("max_pages" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_paging_loop_unauthenticated_raises():
    inst = ComdirectClient()
    inst._secondary_token = None
    inst.access_token = None
    with pytest.raises(RuntimeError, match="Not authenticated"):
        await inst.get_depot_transactions("DEPOT-1")


@pytest.mark.asyncio
async def test_paging_loop_500_propagates(client):
    """Real server errors still raise — only 422 on follow-ups is soft."""
    with _patch_httpx_sequence([_FakeResponse(500, text="boom")]):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_depot_transactions("DEPOT-1")


@pytest.mark.asyncio
async def test_paging_loop_min_booking_date_propagated(client):
    """`min-bookingDate` is forwarded on every page (not just the first)."""
    page1 = {
        "paging": {"index": 0, "matches": 4},
        "values": [_tx(i) for i in range(2)],
    }
    page2 = {
        "paging": {"index": 2, "matches": 4},
        "values": [_tx(i) for i in range(2, 4)],
    }
    captured: list = []

    with _patch_httpx_sequence([_FakeResponse(200, page1), _FakeResponse(200, page2)], captured):
        await client.get_depot_transactions(
            "DEPOT-1", limit=2, min_booking_date="2025-01-01"
        )

    assert all(c["params"]["min-bookingDate"] == "2025-01-01" for c in captured)


@pytest.mark.asyncio
async def test_paging_loop_handles_missing_paging_block(client):
    """Some responses omit the `paging` block; loop falls back to len(values)."""
    only_page = {
        "values": [_tx(i) for i in range(3)],
    }
    captured: list = []

    with _patch_httpx_sequence([_FakeResponse(200, only_page)], captured):
        result = await client.get_depot_transactions("DEPOT-1", limit=10)

    # Short page (3 < 10) → loop terminates after page 1.
    assert len(result) == 3
    assert len(captured) == 1
