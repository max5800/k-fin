"""Transactions router for the Finance API."""

import csv
import io
import json
from datetime import date, datetime, timezone
from typing import Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, select
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
        is_refund=tx.is_refund,
        created_at=tx.created_at,
        updated_at=tx.updated_at,
    )


# Cap the number of tag IDs accepted per request. ``tag_id IN (...)`` against
# an unbounded list is a cheap-but-effective DoS — 10k tags expand into a
# huge SQL parameter list and degrade the planner. 50 is well above any
# realistic UI use (the tag picker lists ~10) and gives a generous margin
# for power users / scripted exports.
_MAX_TAG_IDS = 50


def _apply_filters(
    stmt: Select,
    *,
    date_from: date | None,
    date_to: date | None,
    category_id: str | None,
    tag_ids: list[str] | None,
    is_recurring: bool | None,
    is_outlier: bool | None,
    internal_transfer: bool | None,
    is_refund: bool | None,
    search: str | None,
) -> Select:
    if tag_ids is not None and len(tag_ids) > _MAX_TAG_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"tag_ids accepts at most {_MAX_TAG_IDS} values "
                f"(got {len(tag_ids)})"
            ),
        )
    if date_from:
        stmt = stmt.where(NormalizedTransaction.booking_date >= date_from)
    if date_to:
        stmt = stmt.where(NormalizedTransaction.booking_date <= date_to)
    if category_id is not None:
        stmt = stmt.where(NormalizedTransaction.category_id == category_id)
    if tag_ids:
        # OR-semantics: a tx matches if it has at least one of the
        # requested tags. EXISTS keeps row counts honest even with
        # multiple tag rows per tx (a JOIN would multiply rows and
        # break the count_stmt).
        stmt = stmt.where(
            select(TransactionTag.transaction_id)
            .where(TransactionTag.transaction_id == NormalizedTransaction.id)
            .where(TransactionTag.tag_id.in_(tag_ids))
            .exists()
        )
    if is_recurring is not None:
        stmt = stmt.where(NormalizedTransaction.is_recurring == is_recurring)
    if is_outlier is not None:
        stmt = stmt.where(NormalizedTransaction.is_outlier == is_outlier)
    if internal_transfer is not None:
        stmt = stmt.where(NormalizedTransaction.internal_transfer == internal_transfer)
    if is_refund is not None:
        stmt = stmt.where(NormalizedTransaction.is_refund == is_refund)
    if search:
        # Escape LIKE wildcards from the user-supplied search box so a
        # query like "50%" or "OS_2024" matches the literal characters
        # instead of expanding into a wildcard pattern.
        escaped = (
            search.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        clause = (
            NormalizedTransaction.recipient.ilike(pattern, escape="\\")
            | NormalizedTransaction.sender.ilike(pattern, escape="\\")
            | NormalizedTransaction.description.ilike(pattern, escape="\\")
        )
        stmt = stmt.where(clause)
    return stmt


@router.get("", response_model=TransactionListOut)
def list_transactions(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    category_id: str | None = Query(None),
    tag_ids: list[str] | None = Query(
        None,
        description=(
            "Filter to transactions that have at least one of the given tag IDs "
            "(OR-semantics). Repeat the param: ?tag_ids=a&tag_ids=b."
        ),
    ),
    is_recurring: bool | None = Query(None),
    is_outlier: bool | None = Query(None),
    internal_transfer: bool | None = Query(None),
    is_refund: bool | None = Query(None),
    search: str | None = Query(None),
):
    filter_kwargs = dict(
        date_from=date_from,
        date_to=date_to,
        category_id=category_id,
        tag_ids=tag_ids,
        is_recurring=is_recurring,
        is_outlier=is_outlier,
        internal_transfer=internal_transfer,
        is_refund=is_refund,
        search=search,
    )
    stmt = _apply_filters(select(NormalizedTransaction), **filter_kwargs)
    count_stmt = _apply_filters(
        select(func.count()).select_from(NormalizedTransaction), **filter_kwargs
    )

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


# Cap to keep memory/wall-clock predictable. 50 k rows ~= 6 MB CSV.
_EXPORT_MAX_ROWS = 50_000

_CSV_COLUMNS = (
    "booking_date",
    "value_date",
    "description",
    "counterparty",
    "amount",
    "currency",
    "category",
    "tags",
)


def _row_to_record(tx: NormalizedTransaction, category_name: str | None, tag_names: list[str]) -> dict:
    counterparty = tx.recipient or tx.sender or ""
    return {
        "booking_date": tx.booking_date.isoformat(),
        "value_date": tx.valuation_date.isoformat(),
        "description": tx.description or "",
        "counterparty": counterparty,
        "amount": format(tx.amount, "f"),
        "currency": tx.currency,
        "category": category_name or "",
        "tags": ",".join(tag_names),
    }


def _iter_export_rows(db: Session, stmt: Select) -> Iterator[dict]:
    rows = db.execute(stmt).scalars().all()
    if not rows:
        return iter(())

    cat_ids = {tx.category_id for tx in rows if tx.category_id}
    cat_map: dict[str, str] = {}
    if cat_ids:
        cat_rows = db.execute(select(Category).where(Category.id.in_(cat_ids))).scalars().all()
        cat_map = {c.id: c.name for c in cat_rows}

    tx_ids = [tx.id for tx in rows]
    tag_map: dict[str, list[str]] = {tid: [] for tid in tx_ids}
    if tx_ids:
        tag_rows = db.execute(
            select(TransactionTag.transaction_id, Tag.name)
            .join(Tag, TransactionTag.tag_id == Tag.id)
            .where(TransactionTag.transaction_id.in_(tx_ids))
        ).all()
        for tx_id, tag_name in tag_rows:
            tag_map.setdefault(tx_id, []).append(tag_name)

    def _gen() -> Iterator[dict]:
        for tx in rows:
            yield _row_to_record(tx, cat_map.get(tx.category_id) if tx.category_id else None, tag_map.get(tx.id, []))

    return _gen()


def _csv_stream(records: Iterator[dict]) -> Iterator[str]:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_CSV_COLUMNS))
    writer.writeheader()
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    for record in records:
        writer.writerow(record)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def _json_stream(records: Iterator[dict]) -> Iterator[str]:
    yield "["
    first = True
    for record in records:
        if first:
            first = False
        else:
            yield ","
        yield json.dumps(record, ensure_ascii=False)
    yield "]"


@router.get("/export", include_in_schema=True)
def export_transactions(
    db: Session = Depends(get_db),
    format: Literal["csv", "json"] = Query("csv"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    category_id: str | None = Query(None),
    tag_ids: list[str] | None = Query(None),
    is_recurring: bool | None = Query(None),
    is_outlier: bool | None = Query(None),
    internal_transfer: bool | None = Query(None),
    is_refund: bool | None = Query(None),
    search: str | None = Query(None),
):
    """Stream filtered transactions as CSV or JSON.

    Filters mirror ``GET /transactions``. Capped at ``_EXPORT_MAX_ROWS``
    rows to keep response size bounded.
    """
    stmt = _apply_filters(
        select(NormalizedTransaction),
        date_from=date_from,
        date_to=date_to,
        category_id=category_id,
        tag_ids=tag_ids,
        is_recurring=is_recurring,
        is_outlier=is_outlier,
        internal_transfer=internal_transfer,
        is_refund=is_refund,
        search=search,
    ).order_by(NormalizedTransaction.booking_date.desc(), NormalizedTransaction.id).limit(_EXPORT_MAX_ROWS)

    records = _iter_export_rows(db, stmt)
    today = datetime.now(timezone.utc).date().isoformat()

    if format == "json":
        filename = f"transactions-{today}.json"
        return StreamingResponse(
            _json_stream(records),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    filename = f"transactions-{today}.csv"
    return StreamingResponse(
        _csv_stream(records),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

    if body.is_refund is not None:
        tx.is_refund = body.is_refund
        # Reclassifying a row as a refund counts as a deliberate audit
        # decision — stamp it so the candidate doesn't re-surface in the
        # /review#refunds queue. Explicit refund_audit_decided=False below
        # can still override (e.g. a script clearing all decisions).
        if body.is_refund and body.refund_audit_decided is None:
            tx.refund_audit_decided_at = datetime.now(timezone.utc)

    if body.internal_transfer is not None:
        tx.internal_transfer = body.internal_transfer

    if body.refund_audit_decided is not None:
        tx.refund_audit_decided_at = (
            datetime.now(timezone.utc) if body.refund_audit_decided else None
        )

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
