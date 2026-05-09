"""Pending categorization-suggestion review endpoints.

Suggestions live inside `agent_runs.result.categorization.suggestions`
(JSON). We surface those whose confidence < the user's auto-apply
threshold AND whose transaction is still uncategorized AND has not been
explicitly rejected (`reviewed_suggestions`).
"""

from datetime import date as date_type, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.api.deps import Auth, get_db
from src.core.db.models import (
    AgentRun,
    AppSettings,
    Category,
    NormalizedTransaction,
    ReviewedSuggestion,
    RunStatus,
)

router = APIRouter(
    prefix="/categorization", tags=["categorization"], dependencies=[Auth]
)


class TransactionPreview(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    booking_date: date_type
    amount: Decimal
    sender: str | None
    recipient: str | None
    description: str | None


class PendingSuggestion(BaseModel):
    transaction_id: str
    transaction: TransactionPreview
    suggested_category_id: str
    suggested_category_name: str | None
    confidence: float
    reasoning: str
    # Surfaces the agent's refund hint so the review UI can render a
    # "Erstattung" indicator and persist it on accept. Defaults False so
    # historical runs (pre-2026-05-08) that lack the field don't fail.
    is_refund: bool = False


class PendingResponse(BaseModel):
    run_id: str | None
    threshold: float
    items: list[PendingSuggestion]


class AcceptBody(BaseModel):
    category_id: str | None = None  # if omitted, accept the suggested id
    # Override the refund flag on accept. None = use the agent's value
    # from the suggestion blob; True/False explicitly sets it.
    is_refund: bool | None = None


def _load_threshold(db: Session) -> float:
    row = db.get(AppSettings, 1)
    if row is None:
        return 0.6
    return float(row.auto_apply_confidence)


def _latest_categorization_suggestions(db: Session) -> tuple[str | None, list[dict]]:
    """Return (run_id, suggestions) of the most recent successful run.

    Looks at both single-agent 'categorization' runs and 'full' runs;
    pulls suggestions from result['categorization']['suggestions'] or
    result['suggestions'] depending on shape.
    """
    stmt = (
        select(AgentRun)
        .where(AgentRun.status == RunStatus.SUCCEEDED)
        .where(AgentRun.agent_name.in_(["categorization", "full"]))
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    run = db.execute(stmt).scalar_one_or_none()
    if run is None or not run.result:
        return None, []
    blob = run.result
    if "categorization" in blob and isinstance(blob["categorization"], dict):
        return run.id, blob["categorization"].get("suggestions", []) or []
    return run.id, blob.get("suggestions", []) or []


@router.get("/pending", response_model=PendingResponse)
def list_pending(db: Session = Depends(get_db)) -> PendingResponse:
    threshold = _load_threshold(db)
    run_id, suggestions = _latest_categorization_suggestions(db)
    if not suggestions:
        return PendingResponse(run_id=run_id, threshold=threshold, items=[])

    # Filter: low-confidence + still uncategorized + not previously rejected
    candidates = [s for s in suggestions if float(s.get("confidence", 0)) < threshold]
    if not candidates:
        return PendingResponse(run_id=run_id, threshold=threshold, items=[])

    tx_ids = [s["transaction_id"] for s in candidates]
    tx_rows = db.execute(
        select(NormalizedTransaction).where(NormalizedTransaction.id.in_(tx_ids))
    ).scalars().all()
    tx_by_id = {t.id: t for t in tx_rows if t.category_id is None}

    rejected_ids = set(
        db.execute(
            select(ReviewedSuggestion.transaction_id).where(
                ReviewedSuggestion.transaction_id.in_(tx_ids)
            )
        ).scalars().all()
    )

    cat_rows = db.execute(select(Category)).scalars().all()
    cat_name_by_id = {c.id: c.name for c in cat_rows}

    # Dedupe per transaction_id — keep the highest-confidence suggestion.
    # Defense-in-depth for older runs that were persisted with duplicate
    # entries (the agent now dedupes upstream, but we can't rewrite history).
    best_by_tx: dict[str, dict] = {}
    for s in candidates:
        prev = best_by_tx.get(s["transaction_id"])
        if prev is None or float(s.get("confidence", 0)) > float(prev.get("confidence", 0)):
            best_by_tx[s["transaction_id"]] = s

    items: list[PendingSuggestion] = []
    for tx_id, s in best_by_tx.items():
        tx = tx_by_id.get(tx_id)
        if tx is None or tx.id in rejected_ids:
            continue
        items.append(
            PendingSuggestion(
                transaction_id=tx.id,
                transaction=TransactionPreview.model_validate(tx),
                suggested_category_id=s["suggested_category_id"],
                suggested_category_name=cat_name_by_id.get(s["suggested_category_id"]),
                confidence=float(s["confidence"]),
                reasoning=s.get("reasoning", ""),
                is_refund=bool(s.get("is_refund", False)),
            )
        )

    return PendingResponse(run_id=run_id, threshold=threshold, items=items)


@router.post("/pending/{transaction_id}/accept", status_code=status.HTTP_204_NO_CONTENT)
def accept_pending(
    transaction_id: str,
    body: AcceptBody | None = None,
    db: Session = Depends(get_db),
) -> None:
    tx = db.get(NormalizedTransaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    chosen_id = body.category_id if (body and body.category_id) else None
    suggested_is_refund: bool | None = None
    if chosen_id is None:
        # Pull the suggested category from the latest run
        _, suggestions = _latest_categorization_suggestions(db)
        match = next(
            (s for s in suggestions if s["transaction_id"] == transaction_id), None
        )
        if not match:
            raise HTTPException(
                status_code=400,
                detail="No pending suggestion for this transaction; pass category_id explicitly",
            )
        chosen_id = match["suggested_category_id"]
        suggested_is_refund = bool(match.get("is_refund", False))

    cat = db.get(Category, chosen_id)
    if cat is None:
        raise HTTPException(status_code=400, detail=f"Unknown category: {chosen_id}")

    # Accept-time refund resolution: explicit body value > agent suggestion
    # > unchanged. None means "leave the row's existing is_refund alone"
    # — relevant when the user PATCHes a category but doesn't want to
    # touch the flag they already set manually.
    is_refund_value = (
        body.is_refund if (body and body.is_refund is not None) else suggested_is_refund
    )

    values: dict = {"category_id": chosen_id}
    if is_refund_value is not None:
        values["is_refund"] = is_refund_value

    db.execute(
        update(NormalizedTransaction)
        .where(NormalizedTransaction.id == transaction_id)
        .values(**values)
    )
    db.commit()


@router.post("/pending/{transaction_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_pending(
    transaction_id: str, db: Session = Depends(get_db)
) -> None:
    tx = db.get(NormalizedTransaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    existing = db.get(ReviewedSuggestion, transaction_id)
    if existing is None:
        db.add(
            ReviewedSuggestion(
                transaction_id=transaction_id,
                reason="user-rejected",
                decided_at=datetime.now(),
            )
        )
        db.commit()
