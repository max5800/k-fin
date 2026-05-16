"""PayPalProvider unit tests (M16-P2b).

Covers the PayPal-specific contract behaviour the generic
`test_provider_contract` parametrization does not pin: the
no-TAN `start_sync`, source tagging, and the `window_days` config knob.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.core.db.models import DataSource
from src.external.paypal_provider import DEFAULT_SYNC_WINDOW_DAYS, PayPalProvider
from src.external.provider import CanonicalTransaction, DepotSupport, TanKind


def _provider(config=None) -> tuple[PayPalProvider, AsyncMock]:
    """A PayPalProvider with its client patched out."""
    mock_client = AsyncMock()
    mock_client.get_transactions_range.return_value = []
    with patch(
        "src.external.paypal_provider.PayPalClient", return_value=mock_client
    ):
        provider = PayPalProvider(config=config)
    return provider, mock_client


def test_capability_flags():
    assert PayPalProvider.display_name == "PayPal"
    assert PayPalProvider.supports_depot is DepotSupport.NONE
    assert PayPalProvider.supports_watchlist is False
    assert PayPalProvider.supports_orders is False
    assert PayPalProvider.supports_pending_bookings is False
    assert PayPalProvider.tan_kind is TanKind.NONE_OAUTH_M2M


@pytest.mark.asyncio
async def test_start_sync_returns_none():
    """m2m OAuth needs no user step — start_sync issues no challenge."""
    provider, _ = _provider()
    assert await provider.start_sync() is None


@pytest.mark.asyncio
async def test_complete_sync_tags_source_paypal():
    provider, client = _provider()
    client.get_transactions_range.return_value = [
        {
            "transaction_info": {
                "transaction_id": "TEST-1",
                "transaction_initiation_date": "2026-05-01T08:00:00+0000",
                "transaction_amount": {"currency_code": "EUR", "value": "-5.00"},
                "transaction_subject": "Coffee",
            }
        }
    ]
    stream = [tx async for tx in provider.complete_sync(None)]
    assert len(stream) == 1
    tx = stream[0]
    assert isinstance(tx, CanonicalTransaction)
    assert tx.source is DataSource.PAYPAL
    assert tx.canonical["external_id"] == "TEST-1"
    assert tx.raw_data["transaction_info"]["transaction_id"] == "TEST-1"


@pytest.mark.asyncio
async def test_default_window_is_used():
    provider, client = _provider()
    _ = [tx async for tx in provider.complete_sync(None)]
    (start, end), _ = client.get_transactions_range.call_args
    assert end - start == timedelta(days=DEFAULT_SYNC_WINDOW_DAYS)


@pytest.mark.asyncio
async def test_window_days_config_override():
    provider, client = _provider(config={"window_days": 90})
    _ = [tx async for tx in provider.complete_sync(None)]
    (start, end), _ = client.get_transactions_range.call_args
    assert end - start == timedelta(days=90)
    assert end == date.today()


@pytest.mark.asyncio
async def test_list_accounts_is_empty():
    """PayPal's IBAN-less balance account is not modelled as a bank account."""
    provider, _ = _provider()
    assert await provider.list_accounts() == []
