"""Aggregates router for the Finance API (M6).

Read-only endpoints that compute aggregated financial data via SQL queries.
No DB model changes — pure analytical queries over normalized_transactions.

Refund-aware accounting (introduced 2026-05-08):
  - `income`   = SUM(amount > 0) excluding `is_refund=True` and `internal_transfer=True`
  - `expenses` = SUM(amount < 0) **plus** SUM(`is_refund=True` amounts, which are
    positive and therefore *reduce* the negative expense sum). Refunds belong to
    their original category — Krankenkassen-Erstattung sits on `gesundheit`, not
    on a separate "income" bucket — so per-category sums are net spend out-of-the-box.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, extract, func, select
from sqlalchemy.orm import Session

from src.api.deps import CurrentUser, get_db, require_token
from src.api.schemas import (
    BudgetSpendingItem,
    BudgetSpendingOut,
    CashflowOverTimeOut,
    CashflowPoint,
    CategoryBreakdown,
    MonthlySummaryOut,
    RefundAuditCandidate,
    RefundAuditOut,
    RefundAutoApplyResult,
)
from src.core.db.models import Budget, Category, NormalizedTransaction
from src.services.refund_audit import (
    apply_refund_heuristic,
    list_audit_candidates,
    suggest_refund_category,
)

router = APIRouter(
    prefix="/aggregates",
    tags=["aggregates"],
    dependencies=[Depends(require_token)],
)


# A refund is a positive amount that nullifies a prior expense. It is *not*
# income — folding it back into the same category's expense sum is what
# makes budgets and savings rate match a user's intuition.
_INCOME_CASE = case(
    (
        and_(
            NormalizedTransaction.amount > 0,
            NormalizedTransaction.is_refund.is_(False),
        ),
        NormalizedTransaction.amount,
    )
)
_EXPENSE_CASE = case(
    (NormalizedTransaction.amount < 0, NormalizedTransaction.amount),
    (NormalizedTransaction.is_refund.is_(True), NormalizedTransaction.amount),
)


@router.get("/monthly-summary", response_model=MonthlySummaryOut)
def monthly_summary(
    db: Session = Depends(get_db),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    exclude_internal: bool = Query(True),
):
    """Return income, expenses, net, savings rate, and per-category breakdown for a month."""
    totals_stmt = select(
        func.coalesce(func.sum(_INCOME_CASE), 0).label("income"),
        func.coalesce(func.sum(_EXPENSE_CASE), 0).label("expenses"),
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
    savings_rate = (net / income) if income else 0

    # Per-category breakdown sums *all* amounts in the category (refund +
    # original automatically net out, since both share the same category).
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
        savings_rate=round(savings_rate, 4),
        transaction_count=row.transaction_count,
        by_category=categories,
    )


@router.get("/cashflow-over-time", response_model=CashflowOverTimeOut)
def cashflow_over_time(
    db: Session = Depends(get_db),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    months: int | None = Query(
        None,
        ge=1,
        le=120,
        description="Rolling window: include the last N months ending today. "
        "Mutually exclusive with date_from/date_to (date_from/_to win if set).",
    ),
    exclude_internal: bool = Query(True),
):
    """Return a monthly time-series of income and expenses."""
    if months is not None and date_from is None and date_to is None:
        today = date.today()
        # First day of the month that is (months-1) months before the current one,
        # so `months=12` returns 12 calendar months including the current one.
        year = today.year
        month = today.month - (months - 1)
        while month <= 0:
            month += 12
            year -= 1
        date_from = date(year, month, 1)

    stmt = (
        select(
            extract("year", NormalizedTransaction.booking_date).label("year"),
            extract("month", NormalizedTransaction.booking_date).label("month"),
            func.coalesce(func.sum(_INCOME_CASE), 0).label("income"),
            func.coalesce(func.sum(_EXPENSE_CASE), 0).label("expenses"),
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


@router.get("/budget-spending", response_model=BudgetSpendingOut)
def budget_spending(
    db: Session = Depends(get_db),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """Per-budget consumption for a month, refund-aware.

    For every category that has a `Budget` row, returns:
      - `spent_gross`  — sum of amount<0 transactions (original expenses, ≤ 0)
      - `refunded`     — sum of `is_refund=True` transactions (≥ 0)
      - `spent_net`    — `spent_gross + refunded` (Krankenkasse +30 € erstattet
                          50 € Apothekenrechnung → spent_net = -20 €)
      - `remaining`    — `monthly_limit + spent_net` (positive = budget left)

    Internal transfers are always excluded — they never belong to a budget.
    """
    # Refund sum is computed against `amount` (positive value); the "refund"
    # bucket is conceptually a *reduction* of expenses, so we add it directly
    # to spent_gross to derive spent_net.
    refund_sum = func.coalesce(
        func.sum(
            case((NormalizedTransaction.is_refund.is_(True), NormalizedTransaction.amount))
        ),
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
                NormalizedTransaction.internal_transfer.is_(False),
                extract("year", NormalizedTransaction.booking_date) == year,
                extract("month", NormalizedTransaction.booking_date) == month,
            ),
        )
        .group_by(Budget.category_id, Budget.monthly_limit, Budget.currency, Category.name)
        .order_by(Category.name)
    )

    items = []
    for r in db.execute(stmt).all():
        spent_net = r.spent_gross + r.refunded
        items.append(
            BudgetSpendingItem(
                category_id=r.category_id,
                category_name=r.category_name,
                monthly_limit=r.monthly_limit,
                currency=r.currency,
                spent_gross=r.spent_gross,
                refunded=r.refunded,
                spent_net=spent_net,
                remaining=r.monthly_limit + spent_net,
                transaction_count=r.transaction_count,
            )
        )

    return BudgetSpendingOut(year=year, month=month, items=items)


# ---------------------------------------------------------------------------
# Refund audit (M9-Iter / 2026-05-08) — heuristic + auto-apply live in
# `src.services.refund_audit`. The router maps DB rows → response schema
# and gates the auto-apply endpoint to user-only auth.
# ---------------------------------------------------------------------------


@router.get("/refund-audit", response_model=RefundAuditOut)
def refund_audit(db: Session = Depends(get_db)):
    """List candidates for refund reclassification.

    A *candidate* is a positive-amount transaction currently sitting in
    the `erstattungen` income bucket with `is_refund=False`. After the
    2026-05-08 schema change, true refunds (Krankenkasse, Splitwise,
    Retouren) belong on the original *expense* category with
    `is_refund=True`; only structural income (Steuerrückzahlung,
    Cashback) stays in `erstattungen`.

    The endpoint is read-only: the UI walks the list, the user PATCHes
    each transaction individually via `/transactions/{id}`. No bulk
    write here — irreversible reclassifications shouldn't be a single click.
    """
    rows = list_audit_candidates(db)

    candidates = []
    for tx in rows:
        suggested_id, reason, _auto_apply = suggest_refund_category(
            tx.sender, tx.recipient, tx.description
        )
        candidates.append(
            RefundAuditCandidate(
                id=tx.id,
                booking_date=tx.booking_date,
                amount=tx.amount,
                sender=tx.sender,
                recipient=tx.recipient,
                description=tx.description,
                suggested_category_id=suggested_id,
                suggested_reason=reason,
            )
        )

    return RefundAuditOut(candidates=candidates, total=len(candidates))


@router.post("/refund-audit/auto-apply", response_model=RefundAutoApplyResult)
def refund_audit_auto_apply(
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Run the high-confidence heuristic over all undecided erstattungen-Tx.

    Same logic the API runs on startup — exposed manually so the user can
    re-trigger after editing the heuristic / adding new patterns / pulling
    a fresh sync window without waiting for a pod restart.

    User-only auth: this endpoint mutates historical categorizations
    (sets ``is_refund=True``, swaps the category, stamps the audit
    decision). A stray scheduler running with the static service token
    must not be able to flip rows behind the user's back, so we require
    a JWT-backed :class:`User` principal via :data:`CurrentUser` — the
    dependency raises HTTP 403 for service tokens.
    """
    del current_user  # principal already validated by the dependency
    counts = apply_refund_heuristic(db)
    return RefundAutoApplyResult(**counts)
