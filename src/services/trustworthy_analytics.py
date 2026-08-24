"""Reproducible accounting facts and monthly-review workflow.

This module intentionally does no heuristic relabeling.  It reports the
versioned classifications persisted by normalization, exposes unresolved
amounts as residuals, and gates monthly analysis on explicitly verified source
periods.  Row presence alone is never treated as statement completeness.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RecurringPattern,
    SourceStatementPeriod,
    SubscriptionRecord,
    ValueAssessment,
)
from src.core.config import settings

REPORT_VERSION = 2
ACCOUNTING_CLASSES = (
    "internal_transfer_settlement_parent",
    "financial_asset_building",
    "debt_principal_financing",
    "verified_refund_reimbursement",
    "reconciled_consumption",
    "unresolved_ambiguous",
    "non_outflow_income",
)
SUBSCRIPTION_STATUSES = (
    "booked_payment",
    "active_contract",
    "projected_renewal",
    "variable_service",
    "declined_charge",
    "mail_only_evidence",
    "one_off_candidate",
)
VALUE_CLASSES = (
    "unavoidable_obligation",
    "financial_asset_building",
    "durable_capability_health_home_investment",
    "intentional_experience_joy",
    "convenience",
    "leakage_waste",
)
HIGH_IMPACT_AMOUNT = Decimal("100.00")
_ZERO = Decimal("0.00")


def _month_periods(start: date, end: date) -> list[tuple[date, date]]:
    periods: list[tuple[date, date]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        periods.append((max(first, start), min(last, end)))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return periods


def source_completeness(
    db: Session,
    *,
    start: date,
    end: date,
    sources: Iterable[DataSource],
) -> dict[str, Any]:
    states: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for source in sources:
        for period_start, period_end in _month_periods(start, end):
            row = db.execute(
                select(SourceStatementPeriod)
                .where(SourceStatementPeriod.source == source)
                .where(SourceStatementPeriod.period_start == period_start)
                .where(SourceStatementPeriod.period_end == period_end)
            ).scalar_one_or_none()
            state = "verified_complete"
            if row is not None and row.verified_complete:
                state = "verified_complete"
            elif row is None or not row.rows_present:
                state = "missing"
            else:
                state = "rows_present_unverified"
            item = {
                "source": source.value,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "state": state,
                "rows_present": bool(row and row.rows_present),
                "observed_row_count": row.observed_row_count if row else 0,
                "verified_complete": bool(row and row.verified_complete),
                "verification_method": row.verification_method if row else None,
            }
            states.append(item)
            if state != "verified_complete":
                missing.append(
                    {
                        "source": source.value,
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                        "reason": state,
                    }
                )
    return {"complete": not missing, "periods": states, "missing_periods": missing}


def required_sources(db: Session, *, start: date, end: date) -> list[DataSource]:
    """Resolve server policy plus every active source included in the report."""
    configured = [
        DataSource(value) for value in settings.get_analytics_required_sources()
    ]
    observed = db.execute(
        select(NormalizedTransaction.source)
        .where(NormalizedTransaction.is_active.is_(True))
        .where(NormalizedTransaction.booking_date >= start)
        .where(NormalizedTransaction.booking_date <= end)
        .distinct()
    ).scalars()
    return list(dict.fromkeys([*configured, *observed]))


def accounting_report(db: Session, *, start: date, end: date) -> dict[str, Any]:
    rows = db.execute(
        select(NormalizedTransaction)
        .where(NormalizedTransaction.is_active.is_(True))
        .where(NormalizedTransaction.booking_date >= start)
        .where(NormalizedTransaction.booking_date <= end)
    ).scalars()
    totals = {name: _ZERO for name in ACCOUNTING_CLASSES}
    gross_cash_outflow = _ZERO
    unresolved_outflow = _ZERO
    unresolved_inflow = _ZERO
    transaction_count = 0
    minimum_confidence = Decimal("1.000")
    for tx in rows:
        transaction_count += 1
        amount = Decimal(tx.amount)
        accounting_class = tx.accounting_class
        if accounting_class not in totals:
            accounting_class = "unresolved_ambiguous"
        if amount < 0 and accounting_class != "internal_transfer_settlement_parent":
            gross_cash_outflow += abs(amount)
        if accounting_class == "verified_refund_reimbursement":
            totals[accounting_class] += amount
        elif accounting_class == "unresolved_ambiguous":
            totals[accounting_class] += abs(amount) if amount < 0 else amount
        elif accounting_class == "non_outflow_income":
            totals[accounting_class] += amount
        elif amount < 0:
            totals[accounting_class] += abs(amount)
        if accounting_class == "unresolved_ambiguous":
            if amount < 0:
                unresolved_outflow += abs(amount)
            else:
                unresolved_inflow += amount
        minimum_confidence = min(minimum_confidence, Decimal(tx.accounting_confidence))

    reconciled_net = (
        totals["reconciled_consumption"]
        - totals["verified_refund_reimbursement"]
    )
    confidence = "high"
    if unresolved_outflow or unresolved_inflow:
        confidence = "low"
    elif minimum_confidence < Decimal("0.800"):
        confidence = "medium"
    return {
        "report_version": REPORT_VERSION,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "transaction_count": transaction_count,
        "gross_cash_outflow": gross_cash_outflow,
        "internal_transfer_and_settlement_parent_outflow": totals[
            "internal_transfer_settlement_parent"
        ],
        "financial_asset_building_outflow": totals["financial_asset_building"],
        "distinguishable_debt_principal_financing_outflow": totals[
            "debt_principal_financing"
        ],
        "verified_refunds_reimbursements": totals["verified_refund_reimbursement"],
        "reconciled_consumption_gross": totals["reconciled_consumption"],
        "reconciled_consumption_net": reconciled_net,
        "unresolved_ambiguous_outflow_residual": unresolved_outflow,
        "unresolved_ambiguous_inflow_residual": unresolved_inflow,
        "non_outflow_income": totals["non_outflow_income"],
        "confidence": confidence,
        "minimum_classification_confidence": minimum_confidence,
        "formulas": {
            "gross_cash_outflow": (
                "abs(sum(active amount where amount < 0 and accounting_class != "
                "internal_transfer_settlement_parent)); linked settlement parents "
                "are excluded at the consolidated external-counterparty boundary"
            ),
            "reconciled_consumption_net": (
                "reconciled_consumption_gross - verified_refunds_reimbursements"
            ),
            "outflow_partition": (
                "gross_cash_outflow = financial asset + distinguishable debt + "
                "reconciled consumption gross + unresolved outflow; internal/settlement "
                "parent outflow is reported separately and is not an additive term"
            ),
        },
    }


def subscriptions(db: Session, *, start: date, end: date) -> list[dict[str, Any]]:
    items = [
        {
            "id": row.id,
            "label": row.label,
            "status": row.status,
            "confidence": row.confidence,
            "evidence_source": row.evidence_source,
            "amount_scenarios": row.amount_scenarios or [],
            "scenario_semantics": "discrete_scenarios_not_range_or_contract_truth",
            "next_review_date": row.next_review_date,
        }
        for row in db.execute(
            select(SubscriptionRecord).where(
                or_(
                    SubscriptionRecord.transaction_id.is_(None),
                    exists().where(
                        NormalizedTransaction.id == SubscriptionRecord.transaction_id,
                        NormalizedTransaction.is_active.is_(True),
                    ),
                )
            )
        ).scalars()
        if row.status in SUBSCRIPTION_STATUSES
    ]
    # A recurrence detector proves only booked payments.  It cannot create an
    # active-contract or projected-renewal claim.
    for pattern in db.execute(
        select(RecurringPattern)
        .where(RecurringPattern.last_seen_month >= start.replace(day=1))
        .where(RecurringPattern.first_seen_month <= end)
        .where(
            exists().where(
                NormalizedTransaction.recurring_pattern_id == RecurringPattern.id,
                NormalizedTransaction.is_active.is_(True),
            )
        )
    ).scalars():
        items.append(
            {
                "id": f"recurring-pattern-{pattern.id}",
                "label": pattern.recipient,
                "status": "booked_payment",
                "confidence": Decimal("0.800"),
                "evidence_source": "booked_transaction_recurrence",
                "amount_scenarios": [str(pattern.avg_amount)],
                "scenario_semantics": "discrete_scenarios_not_range_or_contract_truth",
                "next_review_date": None,
            }
        )
    return sorted(items, key=lambda item: (item["label"], item["id"]))


def value_review(db: Session, *, start: date, end: date) -> dict[str, Any]:
    rows = db.execute(
        select(NormalizedTransaction, ValueAssessment)
        .outerjoin(ValueAssessment, NormalizedTransaction.id == ValueAssessment.transaction_id)
        .where(NormalizedTransaction.is_active.is_(True))
        .where(NormalizedTransaction.booking_date >= start)
        .where(NormalizedTransaction.booking_date <= end)
    ).all()
    facts: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    for tx, assessment in rows:
        amount = Decimal(tx.amount)
        if assessment is None:
            if abs(amount) >= HIGH_IMPACT_AMOUNT:
                questions.append(
                    {
                        "transaction_id": tx.id,
                        "question": "How did this high-impact transaction support a declared priority?",
                        "reason": "high_impact_missing_assessment",
                    }
                )
            continue
        item = {
            "assessment_id": assessment.id,
            "value_class": assessment.value_class,
            "confidence": assessment.confidence,
            "declared_priority": assessment.declared_priority,
            "observed_use_count": assessment.observed_use_count,
            "cost_per_use": assessment.cost_per_use,
            "duration_months": assessment.duration_months,
            "duplication": assessment.duplication,
            "cooling_off_regret": assessment.cooling_off_regret,
            "opportunity_cost": assessment.opportunity_cost,
        }
        if assessment.value_class in VALUE_CLASSES:
            facts.append(item)
        if abs(Decimal(amount)) >= HIGH_IMPACT_AMOUNT and Decimal(
            assessment.confidence
        ) < Decimal("0.800"):
            questions.append(
                {
                    "assessment_id": assessment.id,
                    "question": assessment.question
                    or "How did this high-impact purchase support a declared priority?",
                    "reason": "high_impact_low_confidence",
                }
            )
    return {
        "objective": "less_waste_and_more_priority_aligned_value",
        "facts": facts,
        "high_impact_questions": questions,
        "leakage_candidates": [
            item
            for item in facts
            if item["value_class"] == "leakage_waste"
            and Decimal(item["confidence"]) >= Decimal("0.800")
        ],
    }


def monthly_review(db: Session, *, year: int, month: int) -> dict[str, Any]:
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    completeness = source_completeness(
        db, start=start, end=end, sources=required_sources(db, start=start, end=end)
    )
    base = {
        "workflow_version": 2,
        "year": year,
        "month": month,
        "source_completeness": completeness,
        "scheduler_enabled": False,
        "scheduler_note": "Monthly review is manual; no cron or reminder is activated.",
    }
    if not completeness["complete"]:
        return {
            **base,
            "state": "missing_source_periods",
            "can_analyze": False,
            "facts": None,
            "confidence": "blocked_by_source_completeness",
            "high_impact_questions": [],
            "value_review": None,
            "subscriptions": [],
        }
    value = value_review(db, start=start, end=end)
    facts = accounting_report(db, start=start, end=end)
    return {
        **base,
        "state": "analysis_ready",
        "can_analyze": True,
        "facts": facts,
        "confidence": facts["confidence"],
        "high_impact_questions": value["high_impact_questions"],
        "value_review": value,
        "subscriptions": subscriptions(db, start=start, end=end),
    }
