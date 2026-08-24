"""Deterministic financial aggregates shared by API and agents."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, extract, func, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    Budget,
    Category,
    MailEvidence,
    NormalizedTransaction,
    TransactionEvidenceLink,
)

_ZERO = Decimal("0")


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _income_case():
    return case(
        (
            and_(
                NormalizedTransaction.amount > 0,
                NormalizedTransaction.is_refund.is_(False),
            ),
            NormalizedTransaction.amount,
        )
    )


def _expense_case():
    return case(
        (NormalizedTransaction.amount < 0, NormalizedTransaction.amount),
        (NormalizedTransaction.is_refund.is_(True), NormalizedTransaction.amount),
    )


def monthly_summary(
    db: Session,
    *,
    year: int,
    month: int,
    exclude_internal: bool = True,
) -> dict[str, Any]:
    totals_stmt = select(
        func.coalesce(func.sum(_income_case()), 0).label("income"),
        func.coalesce(func.sum(_expense_case()), 0).label("expenses"),
        func.count().label("transaction_count"),
    ).where(
        NormalizedTransaction.is_active.is_(True),
        extract("year", NormalizedTransaction.booking_date) == year,
        extract("month", NormalizedTransaction.booking_date) == month,
    )
    if exclude_internal:
        totals_stmt = totals_stmt.where(NormalizedTransaction.internal_transfer.is_(False))

    row = db.execute(totals_stmt).one()
    income = row.income
    expenses = row.expenses
    net = income + expenses
    savings_rate = (net / income) if income else _ZERO

    cat_stmt = (
        select(
            Category.id,
            Category.name,
            func.sum(NormalizedTransaction.amount).label("total"),
            func.count().label("count"),
        )
        .join(Category, NormalizedTransaction.category_id == Category.id)
        .where(
            NormalizedTransaction.is_active.is_(True),
            extract("year", NormalizedTransaction.booking_date) == year,
            extract("month", NormalizedTransaction.booking_date) == month,
        )
        .group_by(Category.id, Category.name)
        .order_by(func.sum(NormalizedTransaction.amount).asc())
    )
    if exclude_internal:
        cat_stmt = cat_stmt.where(NormalizedTransaction.internal_transfer.is_(False))

    categories = [
        {
            "category_id": r.id,
            "category_name": r.name,
            "total": r.total,
            "transaction_count": r.count,
        }
        for r in db.execute(cat_stmt).all()
    ]
    return {
        "year": year,
        "month": month,
        "income": income,
        "expenses": expenses,
        "net": net,
        "savings_rate": round(savings_rate, 4),
        "transaction_count": row.transaction_count,
        "by_category": categories,
    }


def budget_spending(db: Session, *, year: int, month: int) -> dict[str, Any]:
    refund_sum = func.coalesce(
        func.sum(case((NormalizedTransaction.is_refund.is_(True), NormalizedTransaction.amount))),
        0,
    )
    expense_sum = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        NormalizedTransaction.amount < 0,
                        NormalizedTransaction.is_refund.is_(False),
                    ),
                    NormalizedTransaction.amount,
                )
            )
        ),
        0,
    )

    stmt = (
        select(
            Budget.category_id.label("category_id"),
            Budget.monthly_limit.label("monthly_limit"),
            Budget.currency.label("currency"),
            Budget.is_active.label("is_active"),
            Budget.priority.label("priority"),
            Budget.warning_threshold.label("warning_threshold"),
            Budget.critical_threshold.label("critical_threshold"),
            Budget.context_note.label("context_note"),
            Category.name.label("category_name"),
            expense_sum.label("spent_gross"),
            refund_sum.label("refunded"),
            func.count(NormalizedTransaction.id).label("transaction_count"),
        )
        .join(Category, Category.id == Budget.category_id)
        .outerjoin(
            NormalizedTransaction,
            and_(
                NormalizedTransaction.category_id == Budget.category_id,
                NormalizedTransaction.is_active.is_(True),
                NormalizedTransaction.internal_transfer.is_(False),
                extract("year", NormalizedTransaction.booking_date) == year,
                extract("month", NormalizedTransaction.booking_date) == month,
            ),
        )
        .where(Budget.is_active.is_(True))
        .group_by(
            Budget.category_id,
            Budget.monthly_limit,
            Budget.currency,
            Budget.is_active,
            Budget.priority,
            Budget.warning_threshold,
            Budget.critical_threshold,
            Budget.context_note,
            Category.name,
        )
        .order_by(Budget.priority.desc(), Category.name)
    )

    items = []
    for r in db.execute(stmt).all():
        spent_net = r.spent_gross + r.refunded
        utilization = (abs(spent_net) / r.monthly_limit) if r.monthly_limit else _ZERO
        if utilization >= r.critical_threshold:
            status = "critical"
        elif utilization >= r.warning_threshold:
            status = "warning"
        else:
            status = "ok"
        items.append(
            {
                "category_id": r.category_id,
                "category_name": r.category_name,
                "monthly_limit": r.monthly_limit,
                "currency": r.currency,
                "spent_gross": r.spent_gross,
                "refunded": r.refunded,
                "spent_net": spent_net,
                "remaining": r.monthly_limit + spent_net,
                "transaction_count": r.transaction_count,
                "utilization": round(utilization, 4),
                "status": status,
                "priority": r.priority,
                "context_note": r.context_note,
            }
        )

    return {"year": year, "month": month, "items": items}


def category_semantics(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(select(Category).order_by(Category.name)).scalars().all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "type": row.type.value if hasattr(row.type, "value") else str(row.type),
            "kind": row.kind,
            "budgetable": row.budgetable,
            "analysis_group": row.analysis_group,
            "description": row.description,
            "examples": row.examples or [],
            "anti_examples": row.anti_examples or [],
            "llm_hints": row.llm_hints,
        }
        for row in rows
    ]


def _evidence_for_period(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    rows = db.execute(
        select(MailEvidence)
        .where(MailEvidence.document_date >= start)
        .where(MailEvidence.document_date <= end)
        .order_by(MailEvidence.document_date.desc())
        .limit(100)
    ).scalars()
    return [
        {
            "id": row.id,
            "evidence_type": row.evidence_type,
            "merchant_name": row.merchant_name,
            "document_date": row.document_date.isoformat() if row.document_date else None,
            "total_amount": str(row.total_amount) if row.total_amount is not None else None,
            "currency": row.currency,
            "payment_method": row.payment_method,
            "line_items": row.line_items or [],
            "confidence": str(row.confidence),
        }
        for row in rows
    ]


def _top_transactions(db: Session, start: date, end: date, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            NormalizedTransaction,
            Category.name.label("category_name"),
            MailEvidence.id.label("evidence_id"),
            MailEvidence.evidence_type.label("evidence_type"),
            MailEvidence.merchant_name.label("evidence_merchant"),
            TransactionEvidenceLink.confidence.label("evidence_confidence"),
        )
        .outerjoin(Category, NormalizedTransaction.category_id == Category.id)
        .outerjoin(
            TransactionEvidenceLink,
            TransactionEvidenceLink.transaction_id == NormalizedTransaction.id,
        )
        .outerjoin(MailEvidence, MailEvidence.id == TransactionEvidenceLink.evidence_id)
        .where(NormalizedTransaction.booking_date >= start)
        .where(NormalizedTransaction.booking_date <= end)
        .where(NormalizedTransaction.is_active.is_(True))
        .where(NormalizedTransaction.internal_transfer.is_(False))
        .where(NormalizedTransaction.amount < 0)
        .order_by(NormalizedTransaction.amount.asc())
        .limit(limit)
    ).all()
    return [
        {
            "transaction_id": tx.id,
            "booking_date": tx.booking_date.isoformat(),
            "amount": str(tx.amount),
            "source": tx.source.value if hasattr(tx.source, "value") else str(tx.source),
            "category_id": tx.category_id,
            "category_name": category_name,
            "recipient": tx.recipient,
            "description": tx.description,
            "evidence": {
                "id": evidence_id,
                "type": evidence_type,
                "merchant": evidence_merchant,
                "confidence": str(evidence_confidence) if evidence_confidence is not None else None,
            }
            if evidence_id
            else None,
        }
        for tx, category_name, evidence_id, evidence_type, evidence_merchant, evidence_confidence in rows
    ]


def analysis_context(db: Session, *, year: int, month: int) -> dict[str, Any]:
    start, end = month_bounds(year, month)
    summary = monthly_summary(db, year=year, month=month)
    budgets = budget_spending(db, year=year, month=month)
    budget_items = budgets["items"]
    budget_risks = [
        item
        for item in budget_items
        if item["status"] in {"warning", "critical"} or item["remaining"] < 0
    ]

    uncategorized_count = db.execute(
        select(func.count())
        .select_from(NormalizedTransaction)
        .where(NormalizedTransaction.booking_date >= start)
        .where(NormalizedTransaction.booking_date <= end)
        .where(NormalizedTransaction.is_active.is_(True))
        .where(NormalizedTransaction.category_id.is_(None))
        .where(NormalizedTransaction.is_active.is_(True))
        .where(NormalizedTransaction.internal_transfer.is_(False))
    ).scalar_one()

    return {
        "year": year,
        "month": month,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "monthly_summary": summary,
        "budget_spending": budgets,
        "budget_risks": budget_risks,
        "uncategorized_count": uncategorized_count,
        "category_semantics": category_semantics(db),
        "mail_evidence": _evidence_for_period(db, start, end),
        "top_transactions": _top_transactions(db, start, end),
        "assumptions": [
            "internal transfers are excluded from spend",
            "is_refund=true credits reduce original expense categories",
            "mail evidence is sanitized and matched by amount/date/merchant/payment hints",
        ],
    }
