"""Audit-history regressions for the remitter re-ingest maintenance script."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from scripts.reingest_comdirect_remitter import (
    _carry_fields_and_refresh_accounting,
    _collect_carry_fields,
    _reingest_atomically,
)
from src.core.db.models import (
    Category,
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    SyncRun,
    TransactionLink,
    TypeEnum,
)
from src.normalization.canonicalize import canonicalize, content_hash
from src.normalization.pipeline import NormalizationPipeline


def _tx(tx_id: str, raw_hash: str, *, active: bool = True) -> NormalizedTransaction:
    return NormalizedTransaction(
        id=tx_id,
        raw_content_hash=raw_hash,
        booking_date=date(2026, 1, 1),
        valuation_date=date(2026, 1, 1),
        amount=Decimal("1.00"),
        currency="EUR",
        is_active=active,
        normalization_status="active" if active else "superseded",
        is_recurring=False,
        is_outlier=False,
        internal_transfer=False,
        accounting_version=1,
    )


def test_carry_preserves_predecessor_and_link_history_and_refreshes_successor(
    db_engine,
):
    old_hash = "4" * 64
    successor_hash = "5" * 64
    child_hash = "6" * 64
    decided_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    with Session(db_engine) as session:
        session.add(Category(id="groceries", name="Groceries", type=TypeEnum.VARIABEL))
        session.add_all(
            [
                RawTransaction(content_hash=old_hash, raw_data={"stub": True}),
                RawTransaction(content_hash=successor_hash, raw_data={"stub": True}),
                RawTransaction(content_hash=child_hash, raw_data={"stub": True}),
            ]
        )
        session.flush()
        predecessor = _tx("predecessor", old_hash)
        predecessor.category_id = "groceries"
        predecessor.is_refund = True
        predecessor.refund_verification_status = "user_verified"
        predecessor.refund_audit_decided_at = decided_at
        session.add_all([predecessor, _tx("linked-child", child_hash)])
        session.flush()
        session.add(
            TransactionLink(
                id="retained-link",
                parent_transaction_id="predecessor",
                child_transaction_id="linked-child",
                link_type="manual",
            )
        )
        session.commit()

        carry = _collect_carry_fields(session, [(old_hash, successor_hash)])
        predecessor.is_active = False
        predecessor.normalization_status = "superseded"
        predecessor.superseded_by_id = successor_hash
        session.add(_tx("successor", successor_hash))
        session.commit()

    with Session(db_engine) as session:
        assert _carry_fields_and_refresh_accounting(session, carry) == 1

    with Session(db_engine) as session:
        predecessor = session.get(NormalizedTransaction, "predecessor")
        successor = session.get(NormalizedTransaction, "successor")
        link = session.get(TransactionLink, "retained-link")
        assert predecessor is not None and predecessor.is_active is False
        assert predecessor.category_id == "groceries"
        assert link is not None
        assert link.parent_transaction_id == "predecessor"
        assert successor.category_id == "groceries"
        assert successor.is_refund is True
        assert successor.refund_verification_status == "user_verified"
        assert successor.refund_audit_decided_at == decided_at
        assert successor.accounting_class == "verified_refund_reimbursement"
        assert successor.accounting_confidence == Decimal("1.000")
        assert successor.accounting_version == 2


@pytest.mark.parametrize("external_id", ["DUMMY-REFERENCE-1", None])
def test_reingest_failure_rolls_back_every_stage_and_retry_is_idempotent(
    db_engine, external_id,
):
    old_data = {
        "reference": "DUMMY-REFERENCE-1",
        "booking_date": "2026-01-01",
        "value_date": "2026-01-01",
        "amount": "25.00",
        "currency": "EUR",
        "debtor_name": "",
        "debtor_iban": "",
        "description": "Dummy reimbursement",
    }
    new_data = {
        **old_data,
        "debtor_name": "John Doe",
        "debtor_iban": "DE00000000000000000000",
    }
    old_hash = content_hash(canonicalize(old_data), source="comdirect")
    new_hash = content_hash(canonicalize(new_data), source="comdirect")
    assert old_hash != new_hash

    with Session(db_engine) as session:
        session.add(Category(id="groceries", name="Groceries", type=TypeEnum.VARIABEL))
        session.add(
            RawTransaction(
                content_hash=old_hash,
                source=DataSource.COMDIRECT,
                external_id=external_id,
                raw_data=old_data,
            )
        )
        session.flush()
        predecessor = _tx(old_hash, old_hash)
        predecessor.category_id = "groceries"
        predecessor.is_refund = True
        predecessor.refund_verification_status = "user_verified"
        predecessor.refund_audit_decided_at = datetime(
            2026, 1, 2, tzinfo=timezone.utc
        )
        session.add(predecessor)
        session.commit()

    reingest = [
        {
            "content_hash": new_hash,
            "raw_data": new_data,
            "source": DataSource.COMDIRECT,
            "external_id": external_id,
            "batch_id": None,
        }
    ]
    transitions = [(old_hash, new_hash)]
    pipeline = NormalizationPipeline(
        db_engine.url.render_as_string(hide_password=False), own_ibans=[]
    )

    def fail_after_normalization(stage: str) -> None:
        if stage == "after_normalization":
            raise RuntimeError("injected remitter re-ingest failure")

    try:
        with pytest.raises(RuntimeError, match="injected remitter re-ingest failure"):
            _reingest_atomically(
                pipeline,
                reingest,
                transitions,
                failure_hook=fail_after_normalization,
            )

        with Session(db_engine) as session:
            assert session.get(RawTransaction, old_hash).superseded_by is None
            assert session.get(RawTransaction, new_hash) is None
            assert session.get(NormalizedTransaction, old_hash).is_active is True
            assert session.get(NormalizedTransaction, new_hash) is None
            assert session.query(SyncRun).count() == 0

        inserted, _run_id, carried = _reingest_atomically(
            pipeline, reingest, transitions
        )
        assert (inserted, carried) == (1, 1)
        with Session(db_engine) as session:
            assert session.get(RawTransaction, old_hash).superseded_by == new_hash
            assert session.get(RawTransaction, new_hash).version == 2
            assert session.get(NormalizedTransaction, old_hash).is_active is False
            successor = session.get(NormalizedTransaction, new_hash)
            assert successor.is_active is True
            assert successor.category_id == "groceries"
            assert successor.is_refund is True
            assert successor.refund_verification_status == "user_verified"
            assert successor.accounting_class == "verified_refund_reimbursement"
            assert session.query(SyncRun).count() == 1

        retry_inserted, _retry_run_id, retry_carried = _reingest_atomically(
            pipeline, reingest, transitions
        )
        assert (retry_inserted, retry_carried) == (0, 0)
        with Session(db_engine) as session:
            assert session.query(RawTransaction).count() == 2
            assert session.query(NormalizedTransaction).count() == 2
            assert session.get(NormalizedTransaction, new_hash).is_refund is True
    finally:
        pipeline.engine.dispose()
