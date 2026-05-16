"""SantanderClient tests (M16-P2c).

The client is exercised end-to-end against an :class:`httpx.MockTransport`
serving the synthetic fixture — real client code path, no network. Covers
the happy path, the auth-failure path, and the defensive drift handling.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.external.santander_client import (
    SantanderAuthError,
    SantanderClient,
    SantanderError,
    SantanderParseError,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_FX = json.loads((_FIXTURES / "santander_cc_transactions.json").read_text())


def _transport(*, session_elevated: bool = True, login_status: int = 200):
    """A MockTransport serving the assumed MySantander contract from fixtures."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            if login_status != 200:
                return httpx.Response(login_status, json={})
            return httpx.Response(200, json=_FX["login_response"])
        if path == "/api/auth/session":
            body = _FX["session_elevated"] if session_elevated else _FX["session_pending"]
            return httpx.Response(200, json=body)
        if path == "/api/credit-cards":
            return httpx.Response(200, json=_FX["cards_response"])
        if path.startswith("/api/credit-cards/") and path.endswith("/transactions"):
            return httpx.Response(200, json=_FX["transactions_response"])
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport | None = None) -> SantanderClient:
    client = SantanderClient(transport=transport or _transport())
    client.username = "test-user"
    client.password = "test-pass"
    return client


@pytest.mark.asyncio
async def test_begin_login_issues_challenge():
    client = _client()
    state = await client.begin_login()
    assert state["challenge_required"] is True
    assert state["challenge_id"] == "TEST-CHALLENGE-1"
    await client.close()


@pytest.mark.asyncio
async def test_session_elevation_states():
    elevated = _client(_transport(session_elevated=True))
    assert await elevated.is_session_elevated() is True
    await elevated.close()

    pending = _client(_transport(session_elevated=False))
    assert await pending.is_session_elevated() is False
    await pending.close()


@pytest.mark.asyncio
async def test_get_cards_and_transactions():
    client = _client()
    cards = await client.get_cards()
    assert len(cards) == 1 and cards[0].last4 == "1111"

    txs = await client.get_card_transactions(cards[0].card_id)
    assert len(txs) == 4
    assert txs[0].transaction_id == "TEST-TX-EUR"
    await client.close()


@pytest.mark.asyncio
async def test_login_rejected_raises_auth_error():
    client = _client(_transport(login_status=401))
    with pytest.raises(SantanderAuthError):
        await client.begin_login()
    await client.close()


@pytest.mark.asyncio
async def test_missing_credentials_raises_auth_error():
    client = SantanderClient(transport=_transport())  # no username/password
    with pytest.raises(SantanderAuthError, match="credentials missing"):
        await client.begin_login()
    await client.close()


@pytest.mark.asyncio
async def test_drifted_response_raises_parse_error():
    """A response missing the expected array is treated as structure drift."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(SantanderParseError, match="missing"):
        await client.get_cards()
    await client.close()


@pytest.mark.asyncio
async def test_non_json_response_raises_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(SantanderParseError, match="non-JSON"):
        await client.get_cards()
    await client.close()


@pytest.mark.asyncio
async def test_unverified_endpoints_block_a_real_sync():
    """With no injected transport the client refuses to hit the placeholder host."""
    client = SantanderClient()  # no transport → real-network path
    client.username = "test-user"
    client.password = "test-pass"
    with pytest.raises(SantanderError, match="not yet live-verified"):
        await client.begin_login()
    await client.close()
