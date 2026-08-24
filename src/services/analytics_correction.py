"""Idempotent, count-only correction pass for proven analytics defects."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import (
    AnalyticsCorrectionRun,
    Category,
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    TransactionLink,
)
from src.normalization.pipeline import (
    ACCOUNTING_VERSION,
    AUTO_TRANSACTION_LINK_TYPES,
    NormalizationPipeline,
    _accounting_class_for,
    _is_comdirect_santander_posting,
    _is_paypal_aggregate_posting,
    _is_paypal_bank_deposit,
)

CORRECTION_VERSION = 1


def _authoritative_reconciliation(
    rows: list[NormalizedTransaction],
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    """Re-run the conservative matcher without trusting persisted links."""
    records: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    for tx in rows:
        record = {
            "id": tx.id,
            "source": tx.source,
            "amount": tx.amount,
            "booking_date": tx.booking_date,
            "description": tx.description,
            "sender": tx.sender,
            "recipient": tx.recipient,
            "sender_iban": tx.sender_iban,
            "recipient_iban": tx.recipient_iban,
            "internal_transfer": tx.internal_transfer,
        }
        if (
            tx.source in {DataSource.PAYPAL, DataSource.SANTANDER_CC}
            or _is_paypal_bank_deposit(record)
            or _is_paypal_aggregate_posting(record)
            or _is_comdirect_santander_posting(record)
        ):
            candidate_ids.add(tx.id)
            record["internal_transfer"] = False
        records.append(record)

    if not records:
        return {}, {}
    reconciled, links = NormalizationPipeline._reconcile_cross_source_transfers(
        pd.DataFrame(records)
    )
    expected_links = {str(link["id"]): link for link in links}
    flags = {
        str(row["id"]): bool(row["internal_transfer"])
        for row in reconciled.to_dict("records")
        if str(row["id"]) in candidate_ids
    }
    return expected_links, flags


def _defects(db: Session) -> dict[str, Any]:
    normalized_with_raw = db.execute(
        select(NormalizedTransaction, RawTransaction.superseded_by).join(
            RawTransaction,
            RawTransaction.content_hash == NormalizedTransaction.raw_content_hash,
        )
    ).all()
    stale = [tx for tx, successor in normalized_with_raw if successor and tx.is_active]
    inactive_current = [
        tx for tx, successor in normalized_with_raw if successor is None and not tx.is_active
    ]
    expected_active = [
        tx for tx, successor in normalized_with_raw if successor is None
    ]
    active_ids = {tx.id for tx in expected_active}
    links = db.execute(select(TransactionLink)).scalars().all()
    expected_links, expected_flags = _authoritative_reconciliation(expected_active)
    invalid_links = [
        link
        for link in links
        if link.is_active
        and (
            link.parent_transaction_id == link.child_transaction_id
            or link.parent_transaction_id not in active_ids
            or link.child_transaction_id not in active_ids
            or (
                link.link_type in AUTO_TRANSACTION_LINK_TYPES
                and link.id not in expected_links
            )
        )
    ]
    invalid_link_statuses = {
        link.id: (
            "invalid_participant"
            if link.parent_transaction_id == link.child_transaction_id
            or link.parent_transaction_id not in active_ids
            or link.child_transaction_id not in active_ids
            else "unverified_match"
        )
        for link in invalid_links
    }
    existing_by_id = {link.id: link for link in links}
    links_to_activate = [
        definition
        for link_id, definition in expected_links.items()
        if link_id not in existing_by_id or not existing_by_id[link_id].is_active
    ]
    flag_changes = [
        (tx, expected_flags[tx.id])
        for tx in expected_active
        if tx.id in expected_flags and tx.internal_transfer != expected_flags[tx.id]
    ]
    active_parents = {
        str(definition["parent_transaction_id"])
        for definition in expected_links.values()
    }
    accounting = []
    category_types = dict(db.execute(select(Category.id, Category.type)).all())
    for tx in expected_active:
        original_flag = tx.internal_transfer
        tx.internal_transfer = expected_flags.get(tx.id, original_flag)
        try:
            expected_class, expected_confidence = _accounting_class_for(
                tx,
                tx.id in active_parents,
                category_types.get(tx.category_id),
            )
        finally:
            tx.internal_transfer = original_flag
        if (
            tx.accounting_version != ACCOUNTING_VERSION
            or tx.accounting_class != expected_class
            or Decimal(tx.accounting_confidence) != expected_confidence
        ):
            accounting.append((tx, expected_class, expected_confidence))
    return {
        "stale": stale,
        "inactive_current": inactive_current,
        "invalid_links": invalid_links,
        "invalid_link_statuses": invalid_link_statuses,
        "links_to_activate": links_to_activate,
        "flag_changes": flag_changes,
        "accounting": accounting,
    }


def run_correction(db: Session, *, apply: bool = False) -> dict[str, Any]:
    """Plan by default; apply only explicit reversible status/link changes."""
    defects = _defects(db)
    counts = {
        "stale_normalized_to_supersede": len(defects["stale"]),
        "current_normalized_to_reactivate": len(defects["inactive_current"]),
        "invalid_links_to_deactivate": len(defects["invalid_links"]),
        "authoritative_links_to_activate": len(defects["links_to_activate"]),
        "aggregate_flags_to_refresh": len(defects["flag_changes"]),
        "accounting_classifications_to_refresh": len(defects["accounting"]),
    }
    counts["total_changes"] = sum(counts.values())
    if not apply:
        return {
            "correction_version": CORRECTION_VERSION,
            "mode": "dry-run",
            "applied": False,
            "result_counts": counts,
        }

    if counts["total_changes"] == 0:
        return {
            "correction_version": CORRECTION_VERSION,
            "mode": "apply",
            "applied": False,
            "result_counts": counts,
        }

    for tx in defects["stale"]:
        raw = db.get(RawTransaction, tx.raw_content_hash)
        tx.is_active = False
        tx.normalization_status = "superseded"
        tx.superseded_by_id = raw.superseded_by if raw else None
    for tx in defects["inactive_current"]:
        tx.is_active = True
        tx.normalization_status = "active"
        tx.superseded_by_id = None
    for link in defects["invalid_links"]:
        link.is_active = False
        link.status = defects["invalid_link_statuses"][link.id]
        link.version += 1
    for definition in defects["links_to_activate"]:
        link = db.get(TransactionLink, str(definition["id"]))
        if link is None:
            db.add(
                TransactionLink(
                    **definition,
                    status="active",
                    is_active=True,
                    version=1,
                    confidence=Decimal("1.000"),
                    match_reason="unique exact amount/date reconciliation",
                )
            )
        else:
            link.is_active = True
            link.status = "active"
            link.version += 1
            link.confidence = Decimal("1.000")
            link.match_reason = "unique exact amount/date reconciliation"
    for tx, expected_flag in defects["flag_changes"]:
        tx.internal_transfer = expected_flag
    db.flush()
    for tx, accounting_class, confidence in defects["accounting"]:
        # The defect plan includes rows that this same pass reactivates, so a
        # single explicit apply persists their current accounting immediately.
        tx.accounting_class = accounting_class
        tx.accounting_confidence = confidence
        tx.accounting_version = ACCOUNTING_VERSION

    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(
        AnalyticsCorrectionRun(
            id=run_id,
            correction_version=CORRECTION_VERSION,
            mode="apply",
            status="succeeded",
            result_counts=counts,
            finished_at=now,
        )
    )
    db.commit()
    return {
        "correction_version": CORRECTION_VERSION,
        "mode": "apply",
        "applied": True,
        "audit_run_id": run_id,
        "result_counts": counts,
    }
