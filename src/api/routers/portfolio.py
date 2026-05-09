"""Portfolio router — aggregate KPIs, allocation, performance timeseries.

Fed by the portfolio tables written by `src.normalization.depot_ingest`:
- summary: live totals from `positions` + trailing-12M dividend yield from `depot_transactions`
- allocation: grouped by `instruments.instrument_type`, mapped to user-facing buckets
- performance: read from the aggregated row (depot_id NULL) in `portfolio_snapshots`

Instrument-level price-history endpoints (M11) live alongside because
they share the same prefix; the read-side (`GET .../prices`) is served
straight from Postgres while the write-side (PATCH ticker, POST
backfill-prices) is **dispatched to the worker** so this public-facing
api pod never makes outbound calls to Yahoo Finance and so the
api-pod NetworkPolicy can stay locked down.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import (
    AllocationBucketOut,
    InstrumentOut,
    InstrumentPatch,
    InstrumentPricePointOut,
    PerformancePointOut,
    PortfolioSummaryOut,
    PriceBackfillRequest,
    PriceBackfillResult,
)
from src.core.config import settings
from src.core.db.models import (
    Depot,
    DepotTransaction,
    DepotTransactionType,
    Instrument,
    InstrumentPriceHistory,
    PortfolioSnapshot,
    Position,
)
from src.core.logging import get_logger

logger = get_logger("portfolio")

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


# Short timeout — yfinance roundtrip is single-digit seconds in the
# common case; anything longer indicates either a yfinance hang or
# worker overload, both of which the user wants surfaced quickly.
_BACKFILL_DISPATCH_TIMEOUT_S = 30.0


def _dispatch_backfill_to_worker(payload: dict) -> dict:
    """Forward a price-backfill request to the worker pod.

    The worker owns the yfinance client + the DB write — see
    ``main.py::internal_portfolio_backfill_prices``. This wrapper
    translates worker-side HTTP errors into the same statuses the
    in-process implementation used to raise (404, 400, 502).
    """
    url = settings.worker_url.rstrip("/") + "/internal/portfolio/backfill-prices"
    try:
        resp = httpx.post(
            url, json=payload, timeout=_BACKFILL_DISPATCH_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        logger.error(
            "Worker dispatch /internal/portfolio/backfill-prices failed: %s", exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Worker unreachable: {exc}",
        ) from exc
    if resp.status_code >= 400:
        # Forward the worker's status code where it carries useful
        # information (404 unknown ISIN, 400 missing ticker, 502 yfinance
        # error). Anything else maps to 502 — caller saw a worker fault.
        try:
            detail = resp.json().get("detail", resp.text[:200])
        except ValueError:
            detail = resp.text[:200] or "Worker error"
        forward = (
            resp.status_code
            if resp.status_code in (400, 404, 502)
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=forward, detail=detail)
    return resp.json()


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


# ---------------------------------------------------------------------------
# Instrument metadata + price history (M11)
# ---------------------------------------------------------------------------


@router.patch("/instruments/{isin}", response_model=InstrumentOut)
def patch_instrument(
    isin: str,
    payload: InstrumentPatch,
    db: Session = Depends(get_db),
):
    """Update user-editable fields on an instrument (currently only ticker).

    Comdirect-sourced fields (name, type, currency) are not patchable
    here — they would just be overwritten on the next sync.
    """
    instrument = db.get(Instrument, isin)
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found"
        )

    if payload.ticker_symbol is not None:
        ticker = payload.ticker_symbol.strip() or None
        instrument.ticker_symbol = ticker

    db.commit()
    db.refresh(instrument)
    return InstrumentOut.model_validate(instrument)


@router.post(
    "/instruments/{isin}/backfill-prices",
    response_model=PriceBackfillResult,
)
def backfill_instrument_prices(
    isin: str,
    payload: PriceBackfillRequest,
    db: Session = Depends(get_db),
):
    """Dispatch a price-backfill request to the worker.

    The api pod itself does **not** import yfinance and does **not**
    write to ``instrument_price_history``. The worker handles both — see
    ``main.py::internal_portfolio_backfill_prices``. This keeps the api
    NetworkPolicy tight (no Yahoo Finance egress) and the api image
    free of the yfinance dependency surface.
    """
    if payload.from_date > payload.to_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_date must be <= to_date",
        )

    # Pre-flight existence check so a 404 doesn't have to round-trip
    # through the worker. Cheap, the row is already cached locally.
    instrument = db.get(Instrument, isin)
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found"
        )

    body = _dispatch_backfill_to_worker(
        {
            "isin": isin,
            "from_date": payload.from_date.isoformat(),
            "to_date": payload.to_date.isoformat(),
        }
    )
    return PriceBackfillResult(**body)


@router.get(
    "/instruments/{isin}/prices",
    response_model=list[InstrumentPricePointOut],
)
def list_instrument_prices(
    isin: str,
    db: Session = Depends(get_db),
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
):
    """Return cached daily close prices for an instrument (UI consumption).

    Reads only — does not trigger a backfill. Range is half-open if
    either bound is omitted.
    """
    instrument = db.get(Instrument, isin)
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found"
        )

    stmt = select(InstrumentPriceHistory).where(InstrumentPriceHistory.isin == isin)
    if from_date is not None:
        stmt = stmt.where(InstrumentPriceHistory.price_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(InstrumentPriceHistory.price_date <= to_date)
    stmt = stmt.order_by(InstrumentPriceHistory.price_date)

    rows = db.execute(stmt).scalars().all()
    return [InstrumentPricePointOut.model_validate(r) for r in rows]
