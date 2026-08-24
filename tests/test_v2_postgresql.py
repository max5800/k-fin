"""PostgreSQL-only contracts for the v2 accounting remediation."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import get_report_db
from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    SyncRun,
    SyncStatus,
)
from src.normalization.pipeline import NormalizationPipeline
from src.services.trustworthy_analytics import accounting_report


def _seed_consumption(session: Session) -> None:
    raw_hash = "a" * 64
    session.add(
        RawTransaction(
            content_hash=raw_hash,
            source=DataSource.COMDIRECT,
            raw_data={"stub": True},
        )
    )
    session.flush()
    session.add(
        NormalizedTransaction(
            id=raw_hash,
            raw_content_hash=raw_hash,
            source=DataSource.COMDIRECT,
            booking_date=date(2026, 1, 1),
            valuation_date=date(2026, 1, 1),
            amount=Decimal("-10.00"),
            currency="EUR",
            accounting_class="reconciled_consumption",
            accounting_confidence=Decimal("0.800"),
        )
    )
    session.commit()


def test_report_session_holds_one_repeatable_read_snapshot(db_engine, monkeypatch):
    with Session(db_engine) as session:
        _seed_consumption(session)

    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr("src.core.db.get_session_factory", lambda: factory)
    dependency = get_report_db()
    report_db = next(dependency)
    try:
        assert report_db.execute(text("SHOW transaction_isolation")).scalar_one() == (
            "repeatable read"
        )
        assert report_db.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        first = accounting_report(
            report_db, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )

        with Session(db_engine) as writer:
            tx = writer.get(NormalizedTransaction, "a" * 64)
            tx.amount = Decimal("-20.00")
            writer.commit()

        second = accounting_report(
            report_db, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        assert first["reconciled_consumption_gross"] == Decimal("10.00")
        assert second["reconciled_consumption_gross"] == Decimal("10.00")
    finally:
        with pytest.raises(StopIteration):
            next(dependency)


def test_empty_canonical_frame_is_successful_zero_row_run(db_engine):
    pipeline = NormalizationPipeline(
        db_engine.url.render_as_string(hide_password=False), own_ibans=[]
    )
    try:
        frame, run_id = pipeline.process_and_normalize()
        assert frame.empty
        with Session(db_engine) as session:
            run = session.get(SyncRun, run_id)
            assert run.status == SyncStatus.SUCCEEDED
            assert run.rows_processed == 0
            assert run.error is None
    finally:
        pipeline.engine.dispose()
