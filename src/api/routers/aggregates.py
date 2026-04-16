"""Aggregates router for the Finance API (M6).

Read-only endpoints that compute aggregated financial data via SQL queries.
No DB model changes — pure analytical queries over normalized_transactions.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, extract, func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import (
    CashflowPoint,
    CashflowOverTimeOut,
    CategoryBreakdown,
    MonthlySummaryOut,
)
from src.core.db.models import Category, NormalizedTransaction

router = APIRouter(
    prefix="/aggregates",
    tags=["aggregates"],
    dependencies=[Depends(require_token)],
)


@router.get("/monthly-summary", response_model=MonthlySummaryOut)
def monthly_summary(
    db: Session = Depends(get_db),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    exclude_internal: bool = Query(True),
):
    """Return income, expenses, net, savings rate, and per-category breakdown for a month."""
    base = select(NormalizedTransaction).where(
        extract("year", NormalizedTransaction.booking_date) == year,
        extract("month", NormalizedTransaction.booking_date) == month,
    )
    if exclude_internal:
        base = base.where(NormalizedTransaction.internal_transfer.is_(False))

    # --- totals ---
    totals_stmt = select(
        func.coalesce(
            func.sum(case((NormalizedTransaction.amount > 0, NormalizedTransaction.amount))),
            0,
        ).label("income"),
        func.coalesce(
            func.sum(case((NormalizedTransaction.amount < 0, NormalizedTransaction.amount))),
            0,
        ).label("expenses"),
        func.count().label("transaction_count"),
    ).where(
        extract("year", NormalizedTransaction.booking_date) == year,
        extract("month", NormalizedTransaction.booking_date) == month,
    )
    if exclude_internal:
        totals_stmt = totals_stmt.where(NormalizedTransaction.internal_transfer.is_(False))

    row = db.execute(totals_stmt).one()
    income = row.income
    expenses = row.expenses
    net = income + expenses
    savings_rate = (net / income * 100) if income else 0

    # --- per-category breakdown ---
    cat_stmt = (
        select(
            Category.id,
            Category.name,
            func.sum(NormalizedTransaction.amount).label("total"),
            func.count().label("count"),
        )
        .join(Category, NormalizedTransaction.category_id == Category.id)
        .where(
            extract("year", NormalizedTransaction.booking_date) == year,
            extract("month", NormalizedTransaction.booking_date) == month,
        )
        .group_by(Category.id, Category.name)
        .order_by(func.sum(NormalizedTransaction.amount).asc())
    )
    if exclude_internal:
        cat_stmt = cat_stmt.where(NormalizedTransaction.internal_transfer.is_(False))

    categories = [
        CategoryBreakdown(
            category_id=r.id,
            category_name=r.name,
            total=r.total,
            transaction_count=r.count,
        )
        for r in db.execute(cat_stmt).all()
    ]

    return MonthlySummaryOut(
        year=year,
        month=month,
        income=income,
        expenses=expenses,
        net=net,
        savings_rate=round(savings_rate, 2),
        transaction_count=row.transaction_count,
        by_category=categories,
    )


@router.get("/cashflow-over-time", response_model=CashflowOverTimeOut)
def cashflow_over_time(
    db: Session = Depends(get_db),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    exclude_internal: bool = Query(True),
):
    """Return a monthly time-series of income and expenses."""
    stmt = (
        select(
            extract("year", NormalizedTransaction.booking_date).label("year"),
            extract("month", NormalizedTransaction.booking_date).label("month"),
            func.coalesce(
                func.sum(case((NormalizedTransaction.amount > 0, NormalizedTransaction.amount))),
                0,
            ).label("income"),
            func.coalesce(
                func.sum(case((NormalizedTransaction.amount < 0, NormalizedTransaction.amount))),
                0,
            ).label("expenses"),
            func.count().label("transaction_count"),
        )
        .group_by("year", "month")
        .order_by("year", "month")
    )

    if exclude_internal:
        stmt = stmt.where(NormalizedTransaction.internal_transfer.is_(False))
    if date_from:
        stmt = stmt.where(NormalizedTransaction.booking_date >= date_from)
    if date_to:
        stmt = stmt.where(NormalizedTransaction.booking_date <= date_to)

    rows = db.execute(stmt).all()

    series = [
        CashflowPoint(
            year=int(r.year),
            month=int(r.month),
            income=r.income,
            expenses=r.expenses,
            net=r.income + r.expenses,
            transaction_count=r.transaction_count,
        )
        for r in rows
    ]

    return CashflowOverTimeOut(series=series, total_months=len(series))
