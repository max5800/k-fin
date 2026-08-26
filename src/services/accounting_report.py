"""Owner-scoped, read-only accounting facts over the existing ledger."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    TransactionLink,
)

SETTLEMENT_LINK_SOURCES = {
    "paypal_aggregate": (DataSource.PAYPAL, {DataSource.COMDIRECT, DataSource.SANTANDER_CC}),
    "santander_settlement": (DataSource.SANTANDER_CC, {DataSource.COMDIRECT}),
}
REPORT_CLASSES = (
    "settlement_parent_excluded",
    "internal_transfer_excluded",
    "expense",
    "refund",
    "income",
    "ambiguous",
    "zero_value",
)
MANUAL_SOURCES = {DataSource.PAYPAL, DataSource.SANTANDER_CC}
ZERO = Decimal("0.00")


def _owner_clause(owner_user_id: str):
    """Require explicit attribution; legacy/unattributed rows fail closed."""
    return RawTransaction.raw_data["owner_user_id"].as_string() == owner_user_id


def _active_transactions(
    db: Session, *, owner_user_id: str, sources: list[DataSource]
) -> list[NormalizedTransaction]:
    return list(
        db.execute(
            select(NormalizedTransaction)
            .join(
                RawTransaction,
                RawTransaction.content_hash == NormalizedTransaction.raw_content_hash,
            )
            .where(RawTransaction.superseded_by.is_(None))
            .where(_owner_clause(owner_user_id))
            .where(NormalizedTransaction.source.in_(sources))
        ).scalars()
    )


def _source_freshness(
    db: Session,
    *,
    owner_user_id: str,
    sources: list[DataSource],
    as_of: date,
    freshness_days: int,
) -> dict[str, Any]:
    latest = dict(
        db.execute(
            select(RawTransaction.source, func.max(RawTransaction.created_at))
            .where(_owner_clause(owner_user_id))
            .where(RawTransaction.source.in_(sources))
            .group_by(RawTransaction.source)
        ).all()
    )
    cutoff = datetime.combine(as_of, time.min, tzinfo=timezone.utc) - timedelta(
        days=freshness_days
    )
    states = []
    blocking = []
    for source in sources:
        observed = latest.get(source)
        if observed is not None and observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        state = "missing" if observed is None else "fresh" if observed >= cutoff else "stale"
        item = {
            "source": source.value,
            "manual": source in MANUAL_SOURCES,
            "state": state,
            "last_observed_at": observed,
        }
        states.append(item)
        if state != "fresh":
            blocking.append({"source": source.value, "reason": state})
    return {
        "freshness_days": freshness_days,
        "freshness_ok": not blocking,
        "sources": states,
        "blocking_sources": blocking,
        # Row freshness cannot prove that a provider statement is complete.
        "statement_completeness": "not_proven_by_existing_schema",
        "complete": False,
    }


def _settlement_classes(
    db: Session, transactions: dict[str, NormalizedTransaction]
) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    """Return valid parents, ambiguous rows, and visible graph failures."""
    if not transactions:
        return set(), set(), []
    ids = set(transactions)
    links = list(
        db.execute(
            select(TransactionLink).where(
                or_(
                    TransactionLink.parent_transaction_id.in_(ids),
                    TransactionLink.child_transaction_id.in_(ids),
                )
            )
        ).scalars()
    )
    by_parent: dict[str, list[TransactionLink]] = defaultdict(list)
    child_parent_count = Counter(link.child_transaction_id for link in links)
    for link in links:
        by_parent[link.parent_transaction_id].append(link)

    valid_parents: set[str] = set()
    ambiguous_rows: set[str] = set()
    ambiguities: list[dict[str, Any]] = []
    for parent_id, chain in sorted(by_parent.items()):
        involved = {parent_id, *(link.child_transaction_id for link in chain)}
        reasons: list[str] = []
        parent = transactions.get(parent_id)
        children = [transactions.get(link.child_transaction_id) for link in chain]
        link_types = {link.link_type for link in chain}
        if parent is None or any(child is None for child in children):
            reasons.append("chain_crosses_owner_or_declared_source_scope")
        if len(link_types) != 1 or not link_types.issubset(SETTLEMENT_LINK_SOURCES):
            reasons.append("unsupported_or_mixed_link_type")
        if any(child_parent_count[link.child_transaction_id] != 1 for link in chain):
            reasons.append("child_has_multiple_parents")
        if parent is not None and not parent.internal_transfer:
            reasons.append("parent_not_marked_internal_transfer")
        if parent is not None and all(child is not None for child in children) and len(link_types) == 1:
            child_source, parent_sources = SETTLEMENT_LINK_SOURCES.get(
                next(iter(link_types)), (None, set())
            )
            if parent.source not in parent_sources or any(
                child.source != child_source for child in children
            ):
                reasons.append("source_pattern_mismatch")
            if Decimal(parent.amount) != sum(
                (Decimal(child.amount) for child in children), ZERO
            ):
                reasons.append("amounts_do_not_settle_exactly")
        if reasons:
            scoped_ids = sorted(involved & ids)
            ambiguous_rows.update(scoped_ids)
            ambiguities.append(
                {"transaction_ids": scoped_ids, "reasons": sorted(set(reasons))}
            )
        else:
            valid_parents.add(parent_id)
    return valid_parents, ambiguous_rows, ambiguities


def _classify(
    tx: NormalizedTransaction, *, valid_parents: set[str], ambiguous_rows: set[str]
) -> str:
    amount = Decimal(tx.amount)
    if tx.id in ambiguous_rows:
        return "ambiguous"
    if tx.id in valid_parents:
        return "settlement_parent_excluded"
    if tx.internal_transfer:
        return "internal_transfer_excluded"
    if tx.is_refund and amount > 0:
        return "refund"
    if tx.is_refund or amount == 0:
        return "ambiguous" if tx.is_refund else "zero_value"
    return "expense" if amount < 0 else "income"


def accounting_report(
    db: Session,
    *,
    owner_user_id: str,
    start: date,
    end: date,
    sources: Iterable[DataSource],
    as_of: date,
    freshness_days: int,
) -> dict[str, Any]:
    """Build a provisional report without modifying or reconciling ledger rows."""
    declared_sources = list(dict.fromkeys(sources))
    all_rows = _active_transactions(
        db, owner_user_id=owner_user_id, sources=declared_sources
    )
    by_id = {row.id: row for row in all_rows}
    valid_parents, ambiguous_rows, ambiguities = _settlement_classes(db, by_id)
    period_rows = [row for row in all_rows if start <= row.booking_date <= end]

    buckets = {
        name: {"transaction_count": 0, "outflow": ZERO, "inflow": ZERO, "net": ZERO}
        for name in REPORT_CLASSES
    }
    for tx in period_rows:
        name = _classify(
            tx, valid_parents=valid_parents, ambiguous_rows=ambiguous_rows
        )
        amount = Decimal(tx.amount)
        bucket = buckets[name]
        bucket["transaction_count"] += 1
        bucket["outflow"] += abs(amount) if amount < 0 else ZERO
        bucket["inflow"] += amount if amount > 0 else ZERO
        bucket["net"] += amount

    gross_outflow = sum((bucket["outflow"] for bucket in buckets.values()), ZERO)
    partition_outflow = sum((bucket["outflow"] for bucket in buckets.values()), ZERO)
    freshness = _source_freshness(
        db,
        owner_user_id=owner_user_id,
        sources=declared_sources,
        as_of=as_of,
        freshness_days=freshness_days,
    )
    return {
        "report_version": 1,
        "owner_user_id": owner_user_id,
        "period_start": start,
        "period_end": end,
        "declared_sources": [source.value for source in declared_sources],
        "source_coverage": freshness,
        "analysis_state": "provisional" if freshness["freshness_ok"] else "incomplete_sources",
        "can_claim_complete": False,
        "transaction_count": len(period_rows),
        "classes": buckets,
        "gross_cash_outflow": gross_outflow,
        "partition_outflow": partition_outflow,
        "partition_difference": gross_outflow - partition_outflow,
        "settlement_ambiguities": ambiguities,
        "semantics": {
            "active_selection": "raw revision has superseded_by=null",
            "settlement": "parent excluded only for one exact, source-valid persisted link chain",
            "completeness": "fresh rows do not prove complete provider statements",
        },
    }
