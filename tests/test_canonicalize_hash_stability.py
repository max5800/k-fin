"""Hash-stability regression tests for the canonical content hash.

`content_hash` is the primary key of `raw_transactions` and the FK
target of `normalized_transactions.raw_content_hash` and
`superseded_by`. If the hash for an existing Comdirect transaction ever
drifts, a re-ingest creates a duplicate row instead of being idempotent,
and version chains break.

M16-P1-Follow-up populates `external_id` from the Comdirect API
`reference` (the API leaves `transactionId` empty for booked
transactions). To keep every pre-existing `content_hash` valid, the
comdirect hash forces the identity field to `null` — it is a pure
*content* fingerprint; identity lives in the `external_id` column. These
tests guard that mechanism.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.normalization.canonicalize import (
    CANONICAL_FIELDS_FOR_HASH,
    _COMDIRECT_HASH_IDENTITY_KEY,
    canonicalize,
    content_hash,
)

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


def test_comdirect_hashes_match_frozen_golden_values():
    """Every ground-truth fixture must hash to its frozen value.

    The golden values are verified byte-identical to the pre-Follow-up
    code for real booked transactions (empty `transactionId`) — a diff
    means a hash-stability regression.
    """
    golden = _golden_hashes()
    txs = _ground_truth_transactions()
    assert {t["transaction_id"] for t in txs} == set(golden), (
        "fixture set and golden hash set drifted — regenerate "
        "canonical_hashes_golden.json only with a documented migration"
    )
    for tx in txs:
        actual = content_hash(canonicalize(tx), source="comdirect")
        assert actual == golden[tx["transaction_id"]], (
            f"content_hash drift for {tx['transaction_id']}: "
            f"{actual} != {golden[tx['transaction_id']]}"
        )


def test_comdirect_hash_is_independent_of_external_id():
    """The comdirect hash must not change when external_id changes.

    This is *the* guarantee that keeps the 540 production rows valid:
    they were hashed with a null identity field (empty `transactionId`),
    and populating `external_id` from `reference` must not re-hash them.
    """
    base = canonicalize(
        {
            "booking_date": "2026-02-01",
            "amount": -10.0,
            "remittance_info": "test",
        }
    )
    with_null = dict(base, external_id=None)
    with_ref = dict(base, external_id="DUMMYREF0000AAAA/2")
    with_other = dict(base, external_id="DIFFERENT-REF/9")

    h_null = content_hash(with_null, source="comdirect")
    assert content_hash(with_ref, source="comdirect") == h_null
    assert content_hash(with_other, source="comdirect") == h_null


def test_comdirect_hash_blob_uses_null_legacy_identity_key():
    """The comdirect hash blob carries `comdirect_id: null`, not external_id."""
    assert _COMDIRECT_HASH_IDENTITY_KEY == "comdirect_id"

    canonical = canonicalize(
        {"booking_date": "2026-02-01", "amount": -10.0, "reference": "CD-REF/2"}
    )
    # Reproduce the comdirect serialization content_hash performs.
    blob = json.dumps(
        {
            (_COMDIRECT_HASH_IDENTITY_KEY if k == "external_id" else k): (
                None if k == "external_id" else canonical.get(k)
            )
            for k in CANONICAL_FIELDS_FOR_HASH
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    assert '"comdirect_id":null' in blob
    assert '"external_id":' not in blob


def test_non_comdirect_source_includes_external_id_in_hash():
    """PayPal/Santander hash external_id normally — distinct ids differ."""
    base = canonicalize(
        {"booking_date": "2026-02-01", "amount": -10.0, "remittance_info": "test"}
    )
    a = content_hash(dict(base, external_id="PP-1"), source="paypal")
    b = content_hash(dict(base, external_id="PP-2"), source="paypal")
    assert a != b, "non-comdirect sources must distinguish transactions by id"


def test_external_id_sourced_from_reference_when_transaction_id_empty():
    """canonicalize falls back to `reference` for booked comdirect txns."""
    # Nested API shape: transactionId empty, reference present.
    canonical = canonicalize(
        {
            "bookingDate": "2026-02-01",
            "transactionValue": {"value": "-10.00", "unit": "EUR"},
            "reference": "DUMMYREF0000AAAA/2",
            "remittanceInfo": "test",
        }
    )
    assert canonical["external_id"] == "DUMMYREF0000AAAA/2"

    # reference may also live inside the captured source_payload.
    nested = canonicalize(
        {
            "booking_date": "2026-02-01",
            "amount": -10.0,
            "source_payload": {"reference": "DUMMYREF1111BBBB/2"},
        }
    )
    assert nested["external_id"] == "DUMMYREF1111BBBB/2"


def test_hash_is_idempotent_for_repeated_canonicalize():
    """Canonicalize + hash twice on the same input → identical hash."""
    tx = _ground_truth_transactions()[0]
    first = content_hash(canonicalize(tx), source="comdirect")
    second = content_hash(canonicalize(tx), source="comdirect")
    assert first == second
