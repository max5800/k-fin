"""Narrow read-only reporting endpoint."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_db
from src.core.db.models import DataSource
from src.services.accounting_report import accounting_report

router = APIRouter(prefix="/reporting", tags=["reporting"])


@router.get("/accounting")
def get_accounting_report(
    current_user: CurrentUser,
    date_from: date = Query(...),
    date_to: date = Query(...),
    sources: list[DataSource] = Query(..., min_length=1),
    as_of: date = Query(..., description="Deterministic freshness reference date"),
    freshness_days: int = Query(7, ge=0, le=90),
    db: Session = Depends(get_db),
):
    """Return owner-attributed accounting facts without changing ledger state."""
    if date_to < date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_to must not be before date_from",
        )
    return accounting_report(
        db,
        owner_user_id=current_user.id,
        start=date_from,
        end=date_to,
        sources=sources,
        as_of=as_of,
        freshness_days=freshness_days,
    )
