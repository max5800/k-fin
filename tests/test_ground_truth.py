"""End-to-end validation of the normalization pipeline against a
curated ~50-transaction ground-truth set.

Skipped automatically when Docker is not available (testcontainers
cannot start Postgres). Run with:
    uv run pytest tests/test_ground_truth.py -v
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from src.core.db.models import Category, NormalizedTransaction, Rule, TypeEnum
from src.normalization.ingest import ingest_transactions
from src.normalization.pipeline import NormalizationPipeline

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed_catalog(session: Session) -> None:
    for cat in _load_json("seed_categories.json"):
        session.add(Category(id=cat["id"], name=cat["name"], type=TypeEnum(cat["type"])))
    for idx, rule in enumerate(_load_json("seed_rules.json")):
        session.add(
            Rule(
                id=idx + 1,
                regex_pattern=rule["pattern"],
                target_category_id=rule["category"],
                priority=rule["priority"],
            )
        )
    session.commit()


def test_ground_truth_end_to_end(postgres_url, db_engine):
    fixture = _load_json("ground_truth_transactions.json")
    own_ibans = fixture["own_ibans"]
    transactions = fixture["transactions"]
    expected = {tx["transaction_id"]: tx["expected"] for tx in transactions}

    with Session(db_engine) as session:
        _seed_catalog(session)

    pipeline = NormalizationPipeline(postgres_url, own_ibans=own_ibans)

    inserted = ingest_transactions(
        pipeline, transactions, batch_id=f"ground-truth-{uuid.uuid4().hex[:8]}"
    )
    assert inserted == len(transactions)

    df = pipeline.process_and_normalize()
    assert len(df) == len(transactions)

    with Session(db_engine) as session:
        normalized = {
            n.comdirect_id: n
            for n in session.query(NormalizedTransaction).all()
        }

    mismatches: list[str] = []
    for tx_id, exp in expected.items():
        actual = normalized.get(tx_id)
        assert actual is not None, f"missing normalized row for {tx_id}"
        for field in ("category_id", "is_recurring", "is_outlier", "internal_transfer"):
            got = getattr(actual, field)
            want = exp[field]
            if got != want:
                mismatches.append(
                    f"{tx_id}.{field}: expected {want!r}, got {got!r}"
                )

    assert not mismatches, "ground-truth mismatches:\n" + "\n".join(mismatches)


def test_reingest_is_idempotent(postgres_url, db_engine):
    fixture = _load_json("ground_truth_transactions.json")
    own_ibans = fixture["own_ibans"]
    transactions = fixture["transactions"]

    with Session(db_engine) as session:
        _seed_catalog(session)

    pipeline = NormalizationPipeline(postgres_url, own_ibans=own_ibans)
    ingest_transactions(pipeline, transactions, batch_id="first")
    reingest = ingest_transactions(pipeline, transactions, batch_id="second")
    assert reingest == 0, "identical payloads must not create duplicate raw rows"


def test_corrected_transaction_creates_new_version(postgres_url, db_engine):
    fixture = _load_json("ground_truth_transactions.json")
    own_ibans = fixture["own_ibans"]
    originals = fixture["transactions"][:3]

    with Session(db_engine) as session:
        _seed_catalog(session)

    pipeline = NormalizationPipeline(postgres_url, own_ibans=own_ibans)
    ingest_transactions(pipeline, originals, batch_id="orig")

    corrected = dict(originals[0])
    corrected["amount"] = corrected["amount"] - 0.01  # comdirect correction
    ingest_transactions(pipeline, [corrected], batch_id="correction")

    from src.core.db.models import RawTransaction

    with Session(db_engine) as session:
        rows_for_id = (
            session.query(RawTransaction)
            .filter(RawTransaction.comdirect_id == originals[0]["transaction_id"])
            .order_by(RawTransaction.version)
            .all()
        )
        assert len(rows_for_id) == 2
        assert rows_for_id[0].superseded_by == rows_for_id[1].content_hash
        assert rows_for_id[1].superseded_by is None
        assert rows_for_id[1].version == 2
