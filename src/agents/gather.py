"""Data-gathering helpers: DB → plain dicts for agent prompts.

Implements the 'Aggregation calculates' principle — agents receive
pre-computed numbers and never touch the database directly.
All functions take a SQLAlchemy Engine and return serializable dicts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    Category,
    NormalizedTransaction,
    RecurringPattern,
    Report,
)


def _to_float(val: Decimal | float | None) -> float:
    return float(val) if val is not None else 0.0


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


def get_uncategorized_transactions(engine: Engine, limit: int = 200) -> list[dict]:
    """Transactions with no category that are not internal transfers."""
    with Session(engine) as session:
        rows = session.execute(
            select(
                NormalizedTransaction.id,
                NormalizedTransaction.booking_date,
                NormalizedTransaction.amount,
                NormalizedTransaction.sender,
                NormalizedTransaction.recipient,
                NormalizedTransaction.description,
            )
            .where(NormalizedTransaction.category_id.is_(None))
            .where(NormalizedTransaction.internal_transfer == False)  # noqa: E712
            .order_by(NormalizedTransaction.booking_date.desc())
            .limit(limit)
        ).all()
    return [
        {
            "id": r.id,
            "booking_date": r.booking_date.isoformat(),
            "amount": _to_float(r.amount),
            "sender": r.sender or "",
            "recipient": r.recipient or "",
            "description": r.description or "",
        }
        for r in rows
    ]


def get_available_categories(engine: Engine) -> list[dict]:
    """All categories with id, name, type."""
    with Session(engine) as session:
        rows = session.execute(
            select(Category.id, Category.name, Category.type)
        ).all()
    return [
        {"id": r.id, "name": r.name, "type": r.type.value}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Aggregates (mirrors src/api/routers/aggregates.py SQL logic)
# ---------------------------------------------------------------------------


def get_monthly_summary(engine: Engine, months: int = 6) -> list[dict]:
    """Monthly income/expenses/net time series."""
    month_col = func.to_char(NormalizedTransaction.booking_date, "YYYY-MM").label("month")
    stmt = (
        select(
            month_col,
            func.coalesce(
                func.sum(case((NormalizedTransaction.amount > 0, NormalizedTransaction.amount))), 0
            ).label("income"),
            func.coalesce(
                func.sum(case((NormalizedTransaction.amount < 0, NormalizedTransaction.amount))), 0
            ).label("expenses"),
            func.coalesce(func.sum(NormalizedTransaction.amount), 0).label("net"),
            func.count().label("count"),
        )
        .where(NormalizedTransaction.internal_transfer == False)  # noqa: E712
        .group_by(month_col)
        .order_by(month_col.desc())
        .limit(months)
    )
    with Session(engine) as session:
        rows = session.execute(stmt).all()
    return [
        {
            "month": r.month,
            "income": _to_float(r.income),
            "expenses": _to_float(r.expenses),
            "net": _to_float(r.net),
            "count": r.count,
        }
        for r in reversed(rows)
    ]


def get_category_breakdown(
    engine: Engine,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Per-category totals with percentages."""
    stmt = (
        select(
            NormalizedTransaction.category_id,
            Category.name.label("category_name"),
            func.sum(NormalizedTransaction.amount).label("total"),
            func.count().label("count"),
        )
        .outerjoin(Category, NormalizedTransaction.category_id == Category.id)
        .where(NormalizedTransaction.internal_transfer == False)  # noqa: E712
    )
    if date_from:
        stmt = stmt.where(NormalizedTransaction.booking_date >= date_from)
    if date_to:
        stmt = stmt.where(NormalizedTransaction.booking_date <= date_to)
    stmt = stmt.group_by(NormalizedTransaction.category_id, Category.name)

    with Session(engine) as session:
        rows = session.execute(stmt).all()

    grand_expense = sum(abs(float(r.total)) for r in rows if float(r.total) < 0) or 1.0
    return [
        {
            "category_id": r.category_id,
            "category_name": r.category_name or "Unkategorisiert",
            "total": _to_float(r.total),
            "count": r.count,
            "percentage": round(abs(float(r.total)) / grand_expense * 100, 1)
            if float(r.total) < 0
            else 0,
        }
        for r in rows
    ]


def get_recurring_patterns(engine: Engine) -> list[dict]:
    """All detected recurring patterns."""
    with Session(engine) as session:
        rows = session.execute(
            select(RecurringPattern).order_by(RecurringPattern.avg_amount)
        ).scalars().all()
    return [
        {
            "recipient": r.recipient,
            "avg_amount": _to_float(r.avg_amount),
            "occurrence_count": r.occurrence_count,
            "first_seen": r.first_seen_month.isoformat(),
            "last_seen": r.last_seen_month.isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Period-based queries (for analysis + anomaly)
# ---------------------------------------------------------------------------


def get_period_transactions(
    engine: Engine, date_from: date, date_to: date
) -> list[dict]:
    """All transactions in a date range, with category info."""
    stmt = (
        select(
            NormalizedTransaction.id,
            NormalizedTransaction.booking_date,
            NormalizedTransaction.amount,
            NormalizedTransaction.recipient,
            NormalizedTransaction.description,
            NormalizedTransaction.is_outlier,
            NormalizedTransaction.is_recurring,
            NormalizedTransaction.internal_transfer,
            Category.name.label("category_name"),
        )
        .outerjoin(Category, NormalizedTransaction.category_id == Category.id)
        .where(NormalizedTransaction.booking_date >= date_from)
        .where(NormalizedTransaction.booking_date <= date_to)
        .order_by(NormalizedTransaction.booking_date)
    )
    with Session(engine) as session:
        rows = session.execute(stmt).all()
    return [
        {
            "id": r.id,
            "booking_date": r.booking_date.isoformat(),
            "amount": _to_float(r.amount),
            "recipient": r.recipient or "",
            "description": r.description or "",
            "is_outlier": r.is_outlier,
            "is_recurring": r.is_recurring,
            "internal_transfer": r.internal_transfer,
            "category": r.category_name or "Unkategorisiert",
        }
        for r in rows
    ]


def get_new_counterparties(engine: Engine, since_date: date) -> list[str]:
    """Recipients appearing for the first time after since_date."""
    min_date = func.min(NormalizedTransaction.booking_date).label("first_seen")
    subq = (
        select(NormalizedTransaction.recipient, min_date)
        .where(NormalizedTransaction.recipient.isnot(None))
        .where(NormalizedTransaction.recipient != "")
        .group_by(NormalizedTransaction.recipient)
    ).subquery()

    with Session(engine) as session:
        rows = session.execute(
            select(subq.c.recipient).where(subq.c.first_seen >= since_date)
        ).scalars().all()
    return list(rows)


def get_outlier_transactions(
    engine: Engine, date_from: date, date_to: date
) -> list[dict]:
    """Outlier-flagged transactions in a date range."""
    stmt = (
        select(
            NormalizedTransaction.id,
            NormalizedTransaction.booking_date,
            NormalizedTransaction.amount,
            NormalizedTransaction.recipient,
            NormalizedTransaction.description,
            Category.name.label("category_name"),
        )
        .outerjoin(Category, NormalizedTransaction.category_id == Category.id)
        .where(NormalizedTransaction.is_outlier == True)  # noqa: E712
        .where(NormalizedTransaction.booking_date >= date_from)
        .where(NormalizedTransaction.booking_date <= date_to)
    )
    with Session(engine) as session:
        rows = session.execute(stmt).all()
    return [
        {
            "id": r.id,
            "booking_date": r.booking_date.isoformat(),
            "amount": _to_float(r.amount),
            "recipient": r.recipient or "",
            "description": r.description or "",
            "category": r.category_name or "Unkategorisiert",
        }
        for r in rows
    ]


def get_savings_rate(engine: Engine, date_from: date, date_to: date) -> dict:
    """Compute savings rate: (income - expenses) / income for a period."""
    stmt = (
        select(
            func.coalesce(
                func.sum(case((NormalizedTransaction.amount > 0, NormalizedTransaction.amount))), 0
            ).label("income"),
            func.coalesce(
                func.sum(case((NormalizedTransaction.amount < 0, NormalizedTransaction.amount))), 0
            ).label("expenses"),
        )
        .where(NormalizedTransaction.internal_transfer == False)  # noqa: E712
        .where(NormalizedTransaction.booking_date >= date_from)
        .where(NormalizedTransaction.booking_date <= date_to)
    )
    with Session(engine) as session:
        row = session.execute(stmt).one()

    income = _to_float(row.income)
    expenses = _to_float(row.expenses)
    rate = round((income + expenses) / income * 100, 1) if income > 0 else 0.0
    return {
        "income": income,
        "expenses": expenses,
        "net": round(income + expenses, 2),
        "savings_rate_pct": rate,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }


# ---------------------------------------------------------------------------
# Memory: previous agent reports
# ---------------------------------------------------------------------------


def get_recent_reports(engine: Engine, report_type: str, limit: int = 3) -> list[dict]:
    """Fetch the most recent reports of a given type for run-to-run memory."""
    with Session(engine) as session:
        rows = session.execute(
            select(Report.content, Report.created_at)
            .where(Report.report_type == report_type)
            .order_by(Report.created_at.desc())
            .limit(limit)
        ).all()
    return [
        {"content": r.content, "created_at": r.created_at.isoformat()}
        for r in rows
    ]
