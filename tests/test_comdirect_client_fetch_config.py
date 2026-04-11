from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.connector.comdirect_client import ComdirectClient, resolve_booking_date


@pytest.fixture
def client():
    with patch("src.connector.comdirect_client.settings") as mock_settings:
        mock_settings.comdirect_client_id = "test-id"
        mock_settings.comdirect_client_secret = "test-secret"
        mock_settings.comdirect_username = "john.doe"
        mock_settings.comdirect_pin = "12345"
        mock_settings.comdirect_tan_method = "pushTAN"
        inst = ComdirectClient()
        inst.access_token = "secondary-token"
        yield inst


@pytest.mark.asyncio
async def test_get_all_data_uses_default_fetch_limits(client):
    with (
        patch.object(client, "get_accounts", AsyncMock(return_value=[{"accountId": "A1"}])),
        patch.object(client, "get_depots", AsyncMock(return_value=[{"depotId": "D1"}])),
        patch.object(client, "get_transactions", AsyncMock(return_value=[])) as mock_get_transactions,
        patch.object(client, "get_depot_positions", AsyncMock(return_value=[])),
        patch.object(
            client,
            "get_depot_transactions",
            AsyncMock(return_value=[]),
        ) as mock_get_depot_transactions,
    ):
        await client.get_all_data()

    mock_get_transactions.assert_awaited_once_with(
        "A1",
        paging_count=500,
        min_booking_date=None,
    )
    mock_get_depot_transactions.assert_awaited_once_with(
        "D1",
        limit=100,
        min_booking_date=None,
    )


@pytest.mark.asyncio
async def test_get_all_data_accepts_custom_fetch_limits(client):
    with (
        patch.object(client, "get_accounts", AsyncMock(return_value=[{"accountId": "A1"}])),
        patch.object(client, "get_depots", AsyncMock(return_value=[{"depotId": "D1"}])),
        patch.object(client, "get_transactions", AsyncMock(return_value=[])) as mock_get_transactions,
        patch.object(client, "get_depot_positions", AsyncMock(return_value=[])),
        patch.object(
            client,
            "get_depot_transactions",
            AsyncMock(return_value=[]),
        ) as mock_get_depot_transactions,
    ):
        await client.get_all_data(
            account_transaction_limit=1200,
            account_transaction_min_booking_date="-120d",
            depot_transaction_limit=250,
            depot_transaction_min_booking_date="2025-01-01",
        )

    mock_get_transactions.assert_awaited_once_with(
        "A1",
        paging_count=1200,
        min_booking_date="-120d",
    )
    mock_get_depot_transactions.assert_awaited_once_with(
        "D1",
        limit=250,
        min_booking_date="2025-01-01",
    )


# ---- resolve_booking_date -------------------------------------------------


def test_resolve_booking_date_none():
    assert resolve_booking_date(None) is None


def test_resolve_booking_date_empty_string():
    assert resolve_booking_date("") is None


def test_resolve_booking_date_absolute():
    assert resolve_booking_date("2025-06-15") == "2025-06-15"


def test_resolve_booking_date_relative():
    expected = (date.today() - timedelta(days=30)).isoformat()
    assert resolve_booking_date("-30d") == expected


def test_resolve_booking_date_relative_large():
    expected = (date.today() - timedelta(days=365)).isoformat()
    assert resolve_booking_date("-365d") == expected
