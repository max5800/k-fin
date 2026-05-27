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

    df, _run_id = pipeline.process_and_normalize()
    assert len(df) == len(transactions)

    with Session(db_engine) as session:
        normalized = {n.external_id: n for n in session.query(NormalizedTransaction).all()}

    mismatches: list[str] = []
    for tx_id, exp in expected.items():
        actual = normalized.get(tx_id)
        assert actual is not None, f"missing normalized row for {tx_id}"
        for field in ("category_id", "is_recurring", "is_outlier", "internal_transfer"):
            got = getattr(actual, field)
            want = exp[field]
            if got != want:
                mismatches.append(f"{tx_id}.{field}: expected {want!r}, got {got!r}")

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


def test_normalize_preserves_existing_category(postgres_url, db_engine):
    """Regression: re-running process_and_normalize must NOT wipe a
    category_id that was already set by the agent / a manual edit /
    a previous rule match.

    Prod incident 2026-05-01: every sync re-built the dataframe from
    raw_transactions with category_id=None, then the on-conflict upsert
    overwrote agent-set categories with NULL. Fix uses COALESCE so the
    existing value wins on conflict.
    """
    fixture = _load_json("ground_truth_transactions.json")
    own_ibans = fixture["own_ibans"]
    transactions = fixture["transactions"][:3]

    with Session(db_engine) as session:
        _seed_catalog(session)

    pipeline = NormalizationPipeline(postgres_url, own_ibans=own_ibans)
    ingest_transactions(pipeline, transactions, batch_id="initial")
    pipeline.process_and_normalize()

    # Simulate the categorization agent (or a manual UI edit) setting a
    # category that is NOT what the rules would assign.
    with Session(db_engine) as session:
        target = session.query(NormalizedTransaction).first()
        assert target is not None
        target.category_id = "groceries"  # explicit assignment
        session.commit()
        target_id = target.id

    # Re-running normalize (e.g. after a sync, or a backfill) used to
    # wipe the category. With the COALESCE fix it must stay.
    pipeline.process_and_normalize()

    with Session(db_engine) as session:
        after = session.get(NormalizedTransaction, target_id)
        assert after is not None
        assert after.category_id == "groceries", (
            "normalize re-run wiped the explicit category — "
            "this regression cost us the prod-2026-05-01 incident"
        )


def test_normalize_still_applies_rules_to_uncategorized_rows(postgres_url, db_engine):
    """The COALESCE fix must not break the legitimate case: when a row
    has NO category yet, a matching rule should still set one.
    """
    fixture = _load_json("ground_truth_transactions.json")
    own_ibans = fixture["own_ibans"]
    transactions = fixture["transactions"][:3]

    with Session(db_engine) as session:
        _seed_catalog(session)

    pipeline = NormalizationPipeline(postgres_url, own_ibans=own_ibans)
    ingest_transactions(pipeline, transactions, batch_id="initial")
    df, _ = pipeline.process_and_normalize()

    # At least one of the seeded rules must have matched and set a
    # category — otherwise the test is meaningless.
    with Session(db_engine) as session:
        rows = session.query(NormalizedTransaction).all()
        assert any(r.category_id is not None for r in rows), (
            "no rule matched any seeded transaction — fixture/rules drift"
        )


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
            .filter(RawTransaction.external_id == originals[0]["transaction_id"])
            .order_by(RawTransaction.version)
            .all()
        )
        assert len(rows_for_id) == 2
        assert rows_for_id[0].superseded_by == rows_for_id[1].content_hash
        assert rows_for_id[1].superseded_by is None
        assert rows_for_id[1].version == 2


def test_paypal_links_are_idempotent_across_reruns(postgres_url, db_engine):
    """Regression (full-review H5): re-running process_and_normalize must
    produce a byte-stable result.

    A PayPal purchase and its anonymous bank aggregate should produce the same
    parent/child link after every normalize pass (`POST /sync/normalize` re-runs
    it), with the bank aggregate excluded from spend and the PayPal detail row
    still counted.
    """
    from src.core.db.models import DataSource, TransactionLink
    from src.external.provider import CanonicalTransaction
    from src.normalization.canonicalize import canonicalize
    from src.normalization.ingest import ingest_canonical
    from src.normalization.paypal_csv import parse_paypal_csv, paypal_csv_canonicalize

    cd_raw = {
        "transaction_id": "CD-PP-1",
        "booking_date": "2026-05-09",
        "value_date": "2026-05-09",
        "amount": -19.99,
        "currency": "EUR",
        "type_text": "Lastschrift",
        "remittance_info": "PAYPAL",
        "creditor_name": "PAYPAL EUROPE SARL ET CIE",
        "creditor_iban": "",
        "debtor_name": "",
        "debtor_iban": "",
    }
    pp_csv = (
        "Datum,Beschreibung,Währung,Brutto,Transaktionscode,Name\r\n"
        '08.05.2026,Allgemeine Zahlung,EUR,"-19,99",PP-BUY-1,STEAMGAMES\r\n'
    ).encode("utf-8")
    pp_row = parse_paypal_csv(pp_csv)[0]

    pipeline = NormalizationPipeline(postgres_url, own_ibans=[])
    ingest_canonical(
        pipeline,
        [
            CanonicalTransaction(
                canonical=canonicalize(cd_raw),
                raw_data=cd_raw,
                source=DataSource.COMDIRECT,
            ),
            CanonicalTransaction(
                canonical=paypal_csv_canonicalize(pp_row),
                raw_data=pp_row,
                source=DataSource.PAYPAL,
            ),
        ],
        batch_id="h5-idempotency",
    )

    def _snapshot() -> dict:
        pipeline.process_and_normalize()
        with Session(db_engine) as session:
            return {
                "tx": {
                    n.id: (n.source.value, n.recipient, n.description, n.internal_transfer)
                    for n in session.query(NormalizedTransaction).all()
                },
                "links": [
                    (
                        link.parent_transaction_id,
                        link.child_transaction_id,
                        link.link_type,
                    )
                    for link in session.query(TransactionLink).all()
                ],
            }

    first = _snapshot()
    second = _snapshot()
    assert first == second, "process_and_normalize is not idempotent"

    bank_line = next(v for v in first["tx"].values() if v[1] == "PAYPAL EUROPE SARL ET CIE")
    paypal_line = next(v for v in first["tx"].values() if v[1] == "STEAMGAMES")
    assert bank_line[3]
    assert not paypal_line[3]
    assert len(first["links"]) == 1
    assert first["links"][0][2] == "paypal_aggregate"
