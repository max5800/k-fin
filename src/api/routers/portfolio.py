"""Portfolio router — aggregate KPIs, allocation, performance timeseries.

Fed by the portfolio tables written by `src.normalization.depot_ingest`:
- summary: live totals from `positions` + trailing-12M dividend yield from `depot_transactions`
- allocation: grouped by `instruments.instrument_type`, mapped to user-facing buckets
- performance: read from the aggregated row (depot_id NULL) in `portfolio_snapshots`
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import (
    AllocationBucketOut,
    PerformancePointOut,
    PortfolioSummaryOut,
)
from src.core.db.models import (
    Depot,
    DepotTransaction,
    DepotTransactionType,
    Instrument,
    PortfolioSnapshot,
    Position,
)

router = APIRouter(
    prefix="/portfolio",
    tags=["portfolio"],
    dependencies=[Depends(require_token)],
)

ZERO = Decimal("0")

Range = Literal["1D", "1W", "1M", "1Y", "MAX"]

# Comdirect `instrumentType` → user-facing bucket (German, matches mockup).
# Unmapped types fall into "Sonstiges" so nothing disappears silently.
BUCKET_MAP: dict[str, str] = {
    "SHARE": "Aktien",
    "STOCK": "Aktien",
    "ETF": "ETFs",
    "FUND": "ETFs",
    "BOND": "Anleihen",
    "BONDS": "Anleihen",
}


def _bucket_for(instrument_type: str | None) -> str:
    if not instrument_type:
        return "Sonstiges"
    return BUCKET_MAP.get(instrument_type.upper(), "Sonstiges")


@router.get("/summary", response_model=PortfolioSummaryOut)
def portfolio_summary(db: Session = Depends(get_db)):
    positions = db.execute(select(Position)).scalars().all()

    total_value = sum((p.current_value or ZERO for p in positions), ZERO)
    total_purchase = sum((p.purchase_value or ZERO for p in positions), ZERO)
    pnl_abs = total_value - total_purchase
    pnl_rel = (pnl_abs / total_purchase * 100) if total_purchase else ZERO

    daily_ref = ZERO
    daily_current = ZERO
    for p in positions:
        prev = p.prev_day_value
        if prev is None and p.prev_day_price:
            prev = p.prev_day_price * p.quantity
        if prev is None or prev <= 0:
            continue
        daily_ref += prev
        daily_current += p.current_value or ZERO
    daily_pnl_abs = daily_current - daily_ref if daily_ref else ZERO
    daily_pnl_rel = (daily_pnl_abs / daily_ref * 100) if daily_ref else ZERO

    dividend_yield = _dividend_yield_pct(db, total_value)

    depots_count = db.execute(select(func.count()).select_from(Depot)).scalar_one()
    last_as_of = db.execute(select(func.max(Position.as_of))).scalar_one()

    return PortfolioSummaryOut(
        total_value=total_value,
        total_purchase_value=total_purchase,
        total_pnl_abs=pnl_abs,
        total_pnl_rel=pnl_rel,
        daily_pnl_abs=daily_pnl_abs,
        daily_pnl_rel=daily_pnl_rel,
        dividend_yield_pct=dividend_yield,
        positions_count=len(positions),
        depots_count=depots_count,
        last_synced_at=last_as_of,
    )


@router.get("/allocation", response_model=list[AllocationBucketOut])
def portfolio_allocation(db: Session = Depends(get_db)):
    rows = (
        db.execute(
            select(Instrument.instrument_type, func.sum(Position.current_value))
            .select_from(Position)
            .join(Instrument, Position.isin == Instrument.isin)
            .group_by(Instrument.instrument_type)
        )
        .all()
    )

    aggregated: dict[str, Decimal] = {}
    for instrument_type, value in rows:
        bucket = _bucket_for(instrument_type)
        aggregated[bucket] = aggregated.get(bucket, ZERO) + (value or ZERO)

    total = sum(aggregated.values(), ZERO)
    out = [
        AllocationBucketOut(
            bucket=bucket,
            value=value,
            share_pct=(value / total * 100) if total else ZERO,
        )
        for bucket, value in aggregated.items()
        if value > 0
    ]
    out.sort(key=lambda b: b.value, reverse=True)
    return out


@router.get("/performance", response_model=list[PerformancePointOut])
def portfolio_performance(
    db: Session = Depends(get_db),
    range: Range = Query("1Y"),
):
    today = date.today()
    since: date | None
    if range == "1D":
        since = today - timedelta(days=1)
    elif range == "1W":
        since = today - timedelta(days=7)
    elif range == "1M":
        since = today - timedelta(days=31)
    elif range == "1Y":
        since = today - timedelta(days=365)
    else:  # MAX
        since = None

    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.depot_id.is_(None))
    if since is not None:
        stmt = stmt.where(PortfolioSnapshot.snapshot_date >= since)
    stmt = stmt.order_by(PortfolioSnapshot.snapshot_date)

    rows = db.execute(stmt).scalars().all()
    return [
        PerformancePointOut(
            snapshot_date=row.snapshot_date,
            total_value=row.total_value,
            total_purchase_value=row.total_purchase_value,
        )
        for row in rows
    ]


def _dividend_yield_pct(db: Session, total_value: Decimal) -> Decimal:
    if total_value <= 0:
        return ZERO
    since = date.today() - timedelta(days=365)
    total = db.execute(
        select(func.coalesce(func.sum(DepotTransaction.amount), 0)).where(
            DepotTransaction.transaction_type == DepotTransactionType.DIVIDEND.value,
            DepotTransaction.booking_date >= since,
        )
    ).scalar_one()
    return Decimal(str(total)) / total_value * 100
