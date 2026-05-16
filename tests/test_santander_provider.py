"""SantanderProvider unit tests (M16-P2c).

Exercises the provider against a real :class:`SantanderClient` wired to an
:class:`httpx.MockTransport` — the full start_sync → 2FA → complete_sync
lifecycle without network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from src.core.db.models import DataSource
from src.external.provider import CanonicalTransaction, DepotSupport, TanKind
from src.external.santander_client import SantanderAuthError, SantanderClient
from src.external.santander_provider import SantanderProvider

_FX = json.loads(
    (Path(__file__).parent / "fixtures" / "santander_cc_transactions.json").read_text()
)


def _transport(*, session_elevated: bool = True, remembered: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            body = _FX["login_remembered_device"] if remembered else _FX["login_response"]
            return httpx.Response(200, json=body)
        if path == "/api/auth/session":
            body = _FX["session_elevated"] if session_elevated else _FX["session_pending"]
            return httpx.Response(200, json=body)
        if path == "/api/credit-cards":
            return httpx.Response(200, json=_FX["cards_response"])
        if path.startswith("/api/credit-cards/") and path.endswith("/transactions"):
            return httpx.Response(200, json=_FX["transactions_response"])
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _provider(**transport_kw) -> SantanderProvider:
    provider = SantanderProvider()
    client = SantanderClient(transport=_transport(**transport_kw))
    client.username = "test-user"
    client.password = "test-pass"
    provider._client = client
    return provider


def test_capability_flags():
    assert SantanderProvider.display_name == "Santander Kreditkarte"
    assert SantanderProvider.supports_depot is DepotSupport.NONE
    assert SantanderProvider.supports_watchlist is False
    assert SantanderProvider.supports_orders is False
    assert SantanderProvider.supports_pending_bookings is True
    assert SantanderProvider.tan_kind is TanKind.SCRAPING_SESSION


@pytest.mark.asyncio
async def test_start_sync_issues_scraping_challenge():
    provider = _provider()
    challenge = await provider.start_sync()
    assert challenge is not None
    assert challenge.tan_kind is TanKind.SCRAPING_SESSION
    assert challenge.challenge_id == "TEST-CHALLENGE-1"


@pytest.mark.asyncio
async def test_start_sync_remembered_device_skips_2fa():
    provider = _provider(remembered=True)
    assert await provider.start_sync() is None


@pytest.mark.asyncio
async def test_complete_sync_streams_canonical_transactions():
    provider = _provider()
    await provider.start_sync()
    stream = [tx async for tx in provider.complete_sync(None)]

    assert len(stream) == 4
    for tx in stream:
        assert isinstance(tx, CanonicalTransaction)
        assert tx.source is DataSource.SANTANDER_CC

    by_id = {tx.canonical["external_id"]: tx for tx in stream}
    # FX leg survives onto the canonical dict.
    usd = by_id["TEST-TX-USD"]
    assert usd.canonical["original_amount"] == Decimal("-99.00")
    assert usd.canonical["original_currency"] == "USD"


@pytest.mark.asyncio
async def test_complete_sync_unconfirmed_2fa_raises():
    provider = _provider(session_elevated=False)
    await provider.start_sync()
    with pytest.raises(SantanderAuthError, match="not elevated"):
        _ = [tx async for tx in provider.complete_sync(None)]


@pytest.mark.asyncio
async def test_list_accounts_is_empty():
    """A credit card has no IBAN — it is not modelled as a bank account."""
    assert await _provider().list_accounts() == []
