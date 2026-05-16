"""Hash-stability test for the Comdirect adapter (M16-P2a).

The P2a ``ComdirectProvider`` must emit transactions whose
``content_hash`` is byte-identical to the legacy file-import path —
otherwise a re-sync duplicates every historical row and breaks the
``superseded_by`` version chain.

This drives the provider end-to-end (mocked HTTP) over the frozen
ground-truth fixtures and checks each hash against
``canonical_hashes_golden.json`` — the same golden values
``test_canonicalize_hash_stability`` pins for the direct
``canonicalize`` → ``content_hash`` path. Provider == direct path ==
golden, transitively.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.external.comdirect_provider import ComdirectProvider
from src.normalization.canonicalize import canonicalize, content_hash

_FIXTURES = Path(__file__).parent / "fixtures"


def _ground_truth_transactions() -> list[dict]:
    data = json.loads(
        (_FIXTURES / "ground_truth_transactions.json").read_text(encoding="utf-8")
    )
    return data["transactions"]


def _golden_hashes() -> dict[str, str]:
    data = json.loads(
        (_FIXTURES / "canonical_hashes_golden.json").read_text(encoding="utf-8")
    )
    return data["hashes"]


def _provider_with_mocked_data(transactions: list[dict]) -> ComdirectProvider:
    """A ComdirectProvider whose client returns *transactions* from a sync,
    pre-positioned as if start_sync() had already run."""
    mock_client = AsyncMock()
    mock_client.complete_auth.return_value = True
    mock_client.get_all_data.return_value = {
        "accounts": [],
        "transactions": {"DUMMYACC01": transactions},
        "depots": [],
        "depot_positions": {},
        "depot_transactions": {},
    }
    provider = ComdirectProvider()
    provider._client = mock_client
    provider._session_identifier = "sid"
    provider._challenge_id = "cid"
    return provider


@pytest.mark.asyncio
async def test_provider_hashes_match_frozen_golden_values():
    """Every transaction the provider streams hashes to its frozen golden
    value — the adapter must not drift from the canonical path."""
    txs = _ground_truth_transactions()
    golden = _golden_hashes()

    provider = _provider_with_mocked_data(txs)
    streamed = [ct async for ct in provider.complete_sync(None)]

    assert len(streamed) == len(txs), "provider dropped or duplicated transactions"
    for ct in streamed:
        external_id = ct.canonical["external_id"]
        actual = content_hash(ct.canonical, source="comdirect")
        assert actual == golden[external_id], (
            f"provider content_hash drift for {external_id}: "
            f"{actual} != {golden[external_id]}"
        )


@pytest.mark.asyncio
async def test_provider_hash_equals_direct_canonicalize_path():
    """The provider stream and the direct canonicalize() path agree
    transaction-for-transaction — the adapter adds no hashing logic."""
    txs = _ground_truth_transactions()
    provider = _provider_with_mocked_data(txs)
    streamed = [ct async for ct in provider.complete_sync(None)]

    direct = {
        tx["transaction_id"]: content_hash(canonicalize(tx), source="comdirect")
        for tx in txs
    }
    for ct in streamed:
        external_id = ct.canonical["external_id"]
        assert content_hash(ct.canonical, source="comdirect") == direct[external_id]
