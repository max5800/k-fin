"""Depots router — list depots, positions per depot, depot transactions.

Aggregated KPIs / allocation / performance are in `portfolio.py`; this
module stays close to the raw depot/position/transaction entities.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import (
    DepotOut,
    DepotTransactionListOut,
    DepotTransactionOut,
    InstrumentOut,
    PositionOut,
)
from src.core.db.models import (
    Depot,
    DepotTransaction,
    Instrument,
    Position,
)

router = APIRouter(
    prefix="/depots",
    tags=["depots"],
    dependencies=[Depends(require_token)],
)

ZERO = Decimal("0")


def _depot_totals(db: Session, depot_id: str) -> dict[str, Decimal | int | None]:
    positions = (
        db.execute(select(Position).where(Position.depot_id == depot_id))
        .scalars()
        .all()
    )
    total_value = sum((p.current_value or ZERO for p in positions), ZERO)
    total_purchase = sum((p.purchase_value or ZERO for p in positions), ZERO)
    pnl_abs = total_value - total_purchase
    pnl_rel = (pnl_abs / total_purchase * 100) if total_purchase else ZERO
    last_as_of = max((p.as_of for p in positions), default=None)
    return {
        "total_value": total_value,
        "total_purchase_value": total_purchase,
        "total_pnl_abs": pnl_abs,
        "total_pnl_rel": pnl_rel,
        "positions_count": len(positions),
        "last_synced_at": last_as_of,
    }


@router.get("", response_model=list[DepotOut])
def list_depots(db: Session = Depends(get_db)):
    depots = db.execute(select(Depot).order_by(Depot.depot_id)).scalars().all()
    out: list[DepotOut] = []
    for depot in depots:
        totals = _depot_totals(db, depot.depot_id)
        out.append(
            DepotOut(
                depot_id=depot.depot_id,
                depot_type=depot.depot_type,
                currency=depot.currency,
                **totals,  # type: ignore[arg-type]
            )
        )
    return out


@router.get("/{depot_id}/positions", response_model=list[PositionOut])
def list_positions(depot_id: str, db: Session = Depends(get_db)):
    depot = db.get(Depot, depot_id)
    if depot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Depot not found")

    rows = (
        db.execute(
            select(Position, Instrument)
            .join(Instrument, Position.isin == Instrument.isin)
            .where(Position.depot_id == depot_id)
            .order_by(Position.current_value.desc())
        )
        .all()
    )

    total_value = sum((p.current_value or ZERO for p, _ in rows), ZERO)

    out: list[PositionOut] = []
    for position, instrument in rows:
        purchase = position.purchase_value or ZERO
        current = position.current_value or ZERO
        total_pnl_abs = current - purchase
        total_pnl_rel = (total_pnl_abs / purchase * 100) if purchase else ZERO

        prev_value = position.prev_day_value
        if prev_value is None and position.prev_day_price:
            prev_value = position.prev_day_price * position.quantity
        if prev_value and prev_value > 0:
            daily_pnl_abs = current - prev_value
            daily_pnl_rel = daily_pnl_abs / prev_value * 100
        else:
            daily_pnl_abs = ZERO
            daily_pnl_rel = ZERO

        weight = (current / total_value * 100) if total_value else ZERO

        out.append(
            PositionOut(
                depot_id=depot_id,
                instrument=InstrumentOut.model_validate(instrument),
                quantity=position.quantity,
                current_price=position.current_price,
                current_value=current,
                purchase_value=purchase,
                prev_day_price=position.prev_day_price,
                daily_pnl_abs=daily_pnl_abs,
                daily_pnl_rel=daily_pnl_rel,
                total_pnl_abs=total_pnl_abs,
                total_pnl_rel=total_pnl_rel,
                weight_pct=weight,
                currency=position.currency,
                as_of=position.as_of,
            )
        )
    return out


@router.get("/{depot_id}/transactions", response_model=DepotTransactionListOut)
def list_depot_transactions(
    depot_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    depot = db.get(Depot, depot_id)
    if depot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Depot not found")

    count_stmt = select(func.count()).select_from(DepotTransaction).where(
        DepotTransaction.depot_id == depot_id
    )
    total = db.execute(count_stmt).scalar_one()

    rows = (
        db.execute(
            select(DepotTransaction)
            .where(DepotTransaction.depot_id == depot_id)
            .order_by(DepotTransaction.booking_date.desc(), DepotTransaction.transaction_id)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    return DepotTransactionListOut(
        items=[DepotTransactionOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
