"""Provider-contract conformance tests (M16-P2a).

Every provider in ``PROVIDERS`` must satisfy the ``BankProvider``
contract: well-typed capability flags, and the
``start_sync → (optional challenge) → complete_sync → CanonicalTransaction
stream`` lifecycle. Parametrized over the registry so P2b (PayPal) and
P2c (Santander-CC) extend coverage by adding a registry entry plus one
mock factory — no new test scaffolding.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.core.db.models import DataSource
from src.external.provider import (
    BankProvider,
    CanonicalTransaction,
    DepotSupport,
    SyncChallenge,
    TanKind,
)
from src.external.providers import PROVIDERS, get_provider


# ---------------------------------------------------------------------------
# Static contract — every registered provider, no network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", list(PROVIDERS), ids=lambda s: s.value)
def test_registered_provider_satisfies_contract(source: DataSource):
    """Each provider class structurally satisfies ``BankProvider`` and
    declares correctly-typed capability flags."""
    provider_cls = PROVIDERS[source]
    instance = provider_cls()

    assert isinstance(instance, BankProvider)
    assert isinstance(provider_cls.display_name, str) and provider_cls.display_name
    assert isinstance(provider_cls.supports_depot, DepotSupport)
    assert isinstance(provider_cls.supports_watchlist, bool)
    assert isinstance(provider_cls.supports_orders, bool)
    assert isinstance(provider_cls.supports_pending_bookings, bool)
    assert isinstance(provider_cls.tan_kind, TanKind)


def test_registry_keyed_by_datasource():
    """Every registry key is a DataSource; lookup is by enum value."""
    for source, provider_cls in PROVIDERS.items():
        assert isinstance(source, DataSource)
        assert get_provider(source) is provider_cls


def test_get_provider_unknown_source_raises():
    """A source with no registered provider raises ValueError (→ HTTP 404)."""
    unregistered = [s for s in DataSource if s not in PROVIDERS]
    for source in unregistered:
        with pytest.raises(ValueError, match="No provider registered"):
            get_provider(source)


# ---------------------------------------------------------------------------
# Lifecycle — per-provider HTTP mocking. P2b/P2c append one entry each.
# ---------------------------------------------------------------------------


def _mock_comdirect():
    """Patch ComdirectClient so the Comdirect lifecycle runs without network."""
    mock_client = AsyncMock()
    mock_client.begin_auth.return_value = {
        "session_identifier": "sess",
        "challenge_id": "chal",
    }
    mock_client.complete_auth.return_value = True
    mock_client.get_all_data.return_value = {
        "accounts": [],
        "transactions": {
            "DUMMYACC01": [
                {
                    "transaction_id": "T1",
                    "booking_date": "2026-02-01",
                    "value_date": "2026-02-01",
                    "amount": -10.0,
                    "currency": "EUR",
                    "remittance_info": "Test Buchung",
                    "creditor_name": "John Doe",
                }
            ]
        },
        "depots": [],
        "depot_positions": {},
        "depot_transactions": {},
    }
    return patch(
        "src.external.comdirect_provider.ComdirectClient", return_value=mock_client
    )


def _mock_santander():
    """Patch SantanderClient so the Santander lifecycle runs without network."""
    from src.external.santander_models import SantanderCard, SantanderTransaction

    mock_client = AsyncMock()
    mock_client.begin_login.return_value = {
        "challenge_required": True,
        "challenge_id": "TEST-CHAL",
    }
    mock_client.is_session_elevated.return_value = True
    mock_client.get_cards.return_value = [
        SantanderCard.model_validate(
            {"id": "TEST-CARD", "maskedNumber": "1111", "productName": "1plus Visa Card"}
        )
    ]
    mock_client.get_card_transactions.return_value = [
        SantanderTransaction.model_validate(
            {
                "id": "TEST-SANT-1",
                "bookingDate": "2026-02-01",
                "merchant": "Test Merchant",
                "amount": "-15.00",
                "currency": "EUR",
                "cardLast4": "1111",
            }
        )
    ]
    return patch(
        "src.external.santander_provider.SantanderClient", return_value=mock_client
    )


#: Maps a registered source to a context manager that mocks its network.
_LIFECYCLE_MOCKS = {
    DataSource.COMDIRECT: _mock_comdirect,
    DataSource.SANTANDER_CC: _mock_santander,
}


@pytest.mark.parametrize(
    "source",
    [s for s in PROVIDERS if s in _LIFECYCLE_MOCKS],
    ids=lambda s: s.value,
)
@pytest.mark.asyncio
async def test_provider_lifecycle_yields_canonical_stream(source: DataSource):
    """start_sync → optional challenge → complete_sync → canonical stream."""
    with _LIFECYCLE_MOCKS[source]():
        # Construct inside the patch — the provider builds its HTTP client
        # in __init__, so the mock must be in place first.
        provider = get_provider(source)()
        challenge = await provider.start_sync()
        # A provider either issues a well-formed challenge or none at all.
        if challenge is not None:
            assert isinstance(challenge, SyncChallenge)
            assert isinstance(challenge.tan_kind, TanKind)
            assert challenge.challenge_id

        stream = [tx async for tx in provider.complete_sync(None)]

    assert stream, "provider yielded no transactions for the mocked fixture"
    for tx in stream:
        assert isinstance(tx, CanonicalTransaction)
        assert tx.source == source
        # canonical carries the identity fields content_hash consumes.
        assert {"external_id", "amount", "booking_date", "currency"} <= set(
            tx.canonical
        )
        assert isinstance(tx.raw_data, dict)


@pytest.mark.asyncio
async def test_comdirect_complete_sync_before_start_raises():
    """complete_sync must refuse to run before start_sync opened a session."""
    provider = get_provider(DataSource.COMDIRECT)()
    with pytest.raises(RuntimeError, match="start_sync"):
        _ = [tx async for tx in provider.complete_sync(None)]
