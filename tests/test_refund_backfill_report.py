"""Active-history contract for the read-only refund backfill report."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.orm import Session

from scripts import refund_backfill_report
from src.core.db.models import Category, NormalizedTransaction, RawTransaction, TypeEnum


def test_report_excludes_inactive_normalized_history(
    db_engine, monkeypatch, capsys
):
    with Session(db_engine) as session:
        session.add(
            Category(id="erstattungen", name="Refund review", type=TypeEnum.FIX)
        )
        for marker, active in (("7", True), ("8", False)):
            raw_hash = marker * 64
            session.add(RawTransaction(content_hash=raw_hash, raw_data={"stub": True}))
            session.flush()
            session.add(
                NormalizedTransaction(
                    id=f"tx-{marker}",
                    raw_content_hash=raw_hash,
                    booking_date=date(2026, 1, 1),
                    valuation_date=date(2026, 1, 1),
                    amount=Decimal("1.00"),
                    currency="EUR",
                    category_id="erstattungen",
                    is_active=active,
                    normalization_status="active" if active else "superseded",
                    is_recurring=False,
                    is_outlier=False,
                    internal_transfer=False,
                )
            )
        session.commit()

    monkeypatch.setattr(
        refund_backfill_report,
        "settings",
        SimpleNamespace(database_url=db_engine.url.render_as_string(hide_password=False)),
    )
    monkeypatch.setattr(
        "sys.argv", ["refund_backfill_report.py", "--json"]
    )

    assert refund_backfill_report.main() == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == ["tx-7"]
