"""Transactions router for the Finance API."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import (
    CategoryOut,
    TagOut,
    TransactionListOut,
    TransactionOut,
    TransactionUpdate,
)
from src.core.db.models import (
    Category,
    NormalizedTransaction,
    Tag,
    TransactionTag,
)

router = APIRouter(
    prefix="/transactions",
    tags=["transactions"],
    dependencies=[Depends(require_token)],
)


def _enrich(tx: NormalizedTransaction, db: Session) -> TransactionOut:
    category = None
    if tx.category_id:
        cat = db.get(Category, tx.category_id)
        if cat:
            category = CategoryOut.model_validate(cat)

    tag_rows = (
        db.execute(select(Tag).join(TransactionTag).where(TransactionTag.transaction_id == tx.id))
        .scalars()
        .all()
    )
    tags = [TagOut.model_validate(t) for t in tag_rows]

    return TransactionOut(
        id=tx.id,
        comdirect_id=tx.comdirect_id,
        booking_date=tx.booking_date,
        valuation_date=tx.valuation_date,
        amount=tx.amount,
        currency=tx.currency,
        sender=tx.sender,
        recipient=tx.recipient,
        description=tx.description,
        category=category,
        tags=tags,
        is_recurring=tx.is_recurring,
        is_outlier=tx.is_outlier,
        internal_transfer=tx.internal_transfer,
        created_at=tx.created_at,
        updated_at=tx.updated_at,
    )


@router.get("", response_model=TransactionListOut)
def list_transactions(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    category_id: str | None = Query(None),
    is_recurring: bool | None = Query(None),
    is_outlier: bool | None = Query(None),
    internal_transfer: bool | None = Query(None),
    search: str | None = Query(None),
):
    stmt = select(NormalizedTransaction)
    count_stmt = select(func.count()).select_from(NormalizedTransaction)

    if date_from:
        stmt = stmt.where(NormalizedTransaction.booking_date >= date_from)
        count_stmt = count_stmt.where(NormalizedTransaction.booking_date >= date_from)
    if date_to:
        stmt = stmt.where(NormalizedTransaction.booking_date <= date_to)
        count_stmt = count_stmt.where(NormalizedTransaction.booking_date <= date_to)
    if category_id is not None:
        stmt = stmt.where(NormalizedTransaction.category_id == category_id)
        count_stmt = count_stmt.where(NormalizedTransaction.category_id == category_id)
    if is_recurring is not None:
        stmt = stmt.where(NormalizedTransaction.is_recurring == is_recurring)
        count_stmt = count_stmt.where(NormalizedTransaction.is_recurring == is_recurring)
    if is_outlier is not None:
        stmt = stmt.where(NormalizedTransaction.is_outlier == is_outlier)
        count_stmt = count_stmt.where(NormalizedTransaction.is_outlier == is_outlier)
    if internal_transfer is not None:
        stmt = stmt.where(NormalizedTransaction.internal_transfer == internal_transfer)
        count_stmt = count_stmt.where(NormalizedTransaction.internal_transfer == internal_transfer)
    if search:
        pattern = f"%{search}%"
        clause = (
            NormalizedTransaction.recipient.ilike(pattern)
            | NormalizedTransaction.sender.ilike(pattern)
            | NormalizedTransaction.description.ilike(pattern)
        )
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)

    total = db.execute(count_stmt).scalar_one()

    stmt = (
        stmt.order_by(NormalizedTransaction.booking_date.desc(), NormalizedTransaction.id)
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(stmt).scalars().all()

    return TransactionListOut(
        items=[_enrich(tx, db) for tx in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{transaction_id}", response_model=TransactionOut)
def get_transaction(
    transaction_id: str,
    db: Session = Depends(get_db),
):
    tx = db.get(NormalizedTransaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    return _enrich(tx, db)


@router.patch("/{transaction_id}", response_model=TransactionOut)
def update_transaction(
    transaction_id: str,
    body: TransactionUpdate,
    db: Session = Depends(get_db),
):
    tx = db.get(NormalizedTransaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    if body.category_id is not None:
        cat = db.get(Category, body.category_id)
        if not cat:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Category '{body.category_id}' not found",
            )
        tx.category_id = body.category_id

    if body.tags is not None:
        db.execute(
            TransactionTag.__table__.delete().where(TransactionTag.transaction_id == transaction_id)
        )
        for tag_id in body.tags:
            tag = db.get(Tag, tag_id)
            if not tag:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Tag '{tag_id}' not found",
                )
            db.add(TransactionTag(transaction_id=transaction_id, tag_id=tag_id))

    db.commit()
    db.refresh(tx)
    return _enrich(tx, db)
