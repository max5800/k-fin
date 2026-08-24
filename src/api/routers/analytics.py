"""Versioned trustworthy analytics and manual monthly-review workflow."""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_db, get_report_db, require_token
from src.api.schemas import (
    MonthlyReviewOut,
    SourcePeriodVerificationIn,
    SubscriptionRecordIn,
    TrustworthyAccountingReportOut,
    ValueAssessmentIn,
)
from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    SourceStatementPeriod,
    SubscriptionRecord,
    ValueAssessment,
)
from src.services import trustworthy_analytics

router = APIRouter(
    prefix="/analytics/v2",
    tags=["analytics-v2"],
    dependencies=[Depends(require_token)],
)


@router.get("/accounting-report", response_model=TrustworthyAccountingReportOut)
def accounting_report(
    date_from: date = Query(...),
    date_to: date = Query(...),
    db: Session = Depends(get_report_db),
):
    """Return the v2 mutually-exclusive accounting partition over active rows."""
    if date_to < date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_to must not be before date_from",
        )
    return trustworthy_analytics.accounting_report(db, start=date_from, end=date_to)


@router.get("/monthly-review", response_model=MonthlyReviewOut)
def monthly_review(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_report_db),
):
    """Minimal-input UI state: completeness first, then reproducible facts."""
    return trustworthy_analytics.monthly_review(db, year=year, month=month)


@router.put("/source-periods/verification", response_model=dict)
def set_source_period_verification(
    body: SourcePeriodVerificationIn,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Record or reverse an explicit statement-completeness decision.

    This updates local evidence only; it never contacts or mutates a bank.
    Service tokens cannot assert completeness on a user's behalf.
    """
    del current_user
    try:
        source = DataSource(body.source)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unsupported source",
        ) from exc
    expected_end = date(
        body.period_start.year,
        body.period_start.month,
        calendar.monthrange(body.period_start.year, body.period_start.month)[1],
    )
    if body.period_start.day != 1 or body.period_end != expected_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="verification periods must be complete calendar months",
        )
    if body.verification_method not in {"statement_export", "manual_statement_check"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="verification_method must identify an explicit statement check",
        )
    record_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"k-fin:source-period:{source.value}:{body.period_start}:{body.period_end}",
        )
    )
    now = datetime.now(timezone.utc)
    stmt = pg_insert(SourceStatementPeriod).values(
        id=record_id,
        source=source,
        period_start=body.period_start,
        period_end=body.period_end,
        rows_present=False,
        observed_row_count=0,
        verified_complete=body.verified_complete,
        verification_method=body.verification_method,
        verified_at=now if body.verified_complete else None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "verified_complete": body.verified_complete,
            "verification_method": body.verification_method,
            "verified_at": now if body.verified_complete else None,
            "updated_at": now,
        },
    )
    db.execute(stmt)
    db.commit()
    return {
        "source": source.value,
        "period_start": body.period_start.isoformat(),
        "period_end": body.period_end.isoformat(),
        "verified_complete": body.verified_complete,
    }


@router.put("/subscriptions/{record_id}", response_model=dict)
def upsert_subscription_record(
    record_id: str,
    body: SubscriptionRecordIn,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Store itemized evidence state; scenario amounts stay discrete."""
    del current_user
    if body.transaction_id:
        tx = db.get(NormalizedTransaction, body.transaction_id)
        if tx is None or not tx.is_active:
            raise HTTPException(status_code=404, detail="active transaction not found")
    stmt = pg_insert(SubscriptionRecord).values(
        id=record_id,
        label=body.label,
        status=body.status,
        confidence=body.confidence,
        evidence_source=body.evidence_source,
        transaction_id=body.transaction_id,
        amount_scenarios=[str(value) for value in body.amount_scenarios],
        next_review_date=body.next_review_date,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "label": body.label,
            "status": body.status,
            "confidence": body.confidence,
            "evidence_source": body.evidence_source,
            "transaction_id": body.transaction_id,
            "amount_scenarios": [str(value) for value in body.amount_scenarios],
            "next_review_date": body.next_review_date,
        },
    )
    db.execute(stmt)
    db.commit()
    return {"id": record_id, "status": body.status}


@router.put("/value-assessments/{transaction_id}", response_model=dict)
def upsert_value_assessment(
    transaction_id: str,
    body: ValueAssessmentIn,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Record user evidence; ambiguity remains a question, never a relabel."""
    del current_user
    tx = db.get(NormalizedTransaction, transaction_id)
    if tx is None or not tx.is_active:
        raise HTTPException(status_code=404, detail="active transaction not found")
    assessment_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"k-fin:value-assessment:{transaction_id}")
    )
    values = {
        "id": assessment_id,
        "transaction_id": transaction_id,
        **body.model_dump(),
    }
    stmt = pg_insert(ValueAssessment).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["transaction_id"],
        set_=body.model_dump(),
    )
    db.execute(stmt)
    db.commit()
    return {
        "id": assessment_id,
        "transaction_id": transaction_id,
        "value_class": body.value_class,
    }
