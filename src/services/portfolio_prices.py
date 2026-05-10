"""Portfolio price-history service — yfinance fetch + DB upsert.

Encapsulates the M11 backfill pipeline:

  * validate the request (range order, instrument exists, ticker set);
  * fetch daily close prices from a :class:`HistoryProvider` (yfinance);
  * upsert into ``instrument_price_history``, deduped against the
    existing rows in the requested window.

Lives outside ``src/api/routers/portfolio.py`` and ``main.py`` so the
worker endpoint and any future direct caller share one implementation.

Exceptions are framework-neutral; the FastAPI route maps them onto
the right HTTP status codes (400 / 404 / 502). Tests for the worker
endpoint still go through FastAPI to verify the mapping; tests for
the service can call it directly with a fake provider and a real
``Session``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.db.models import Instrument, InstrumentPriceHistory
from src.core.logging import get_logger
from src.external.yfinance_client import (
    CurrencyMismatchError,
    HistoryProvider,
    PriceFetchError,
)

logger = get_logger("portfolio_prices")

PRICE_SOURCE = "yfinance"


# ---------------------------------------------------------------------------
# Domain errors — framework-neutral. The route translates these to HTTP.
# ---------------------------------------------------------------------------


class PortfolioPriceError(Exception):
    """Base for all errors raised out of the portfolio-price service."""


class InvalidPriceRangeError(PortfolioPriceError):
    """Caller passed ``from_date > to_date``. Maps to HTTP 400."""


class InstrumentNotFoundError(PortfolioPriceError):
    """No row in ``instruments`` for the given ISIN. Maps to HTTP 404."""


class TickerNotConfiguredError(PortfolioPriceError):
    """The instrument exists but has no ``ticker_symbol`` — the user must
    PATCH it before yfinance can be queried. Maps to HTTP 400.
    """


class CurrencyMismatchProviderError(PortfolioPriceError):
    """yfinance returned a series in a different currency than the
    instrument is denominated in (e.g. EUR vs. USD). User-actionable —
    the ticker is wrong. Maps to HTTP 400 with the upstream message.
    """


class PriceProviderError(PortfolioPriceError):
    """Upstream price provider (yfinance) failed in a way the user cannot
    fix — rate limit, transient network error, etc. The route maps this
    to 502 and the service deliberately swallows the upstream message
    (security M2: no provider internals leak via the public surface).
    """


@dataclass(frozen=True)
class BackfillResult:
    """Counts + metadata returned from a successful backfill run."""

    isin: str
    ticker_symbol: str
    requested_from: date
    requested_to: date
    fetched_points: int
    inserted_points: int
    skipped_existing: int
    source: str = PRICE_SOURCE


# ---------------------------------------------------------------------------
# Backfill driver
# ---------------------------------------------------------------------------


def backfill_instrument_prices(
    db: Session,
    *,
    isin: str,
    from_date: date,
    to_date: date,
    provider: HistoryProvider,
) -> BackfillResult:
    """Fetch + upsert daily close prices for an instrument.

    Idempotent on overlap: rows already present in the requested window
    are counted in ``skipped_existing`` instead of being re-inserted.
    Empty provider responses are not an error — they yield a result with
    zero counts so the caller sees the round-trip succeeded.

    Errors are raised as typed :class:`PortfolioPriceError` subclasses;
    the FastAPI route is responsible for HTTP mapping.
    """
    if from_date > to_date:
        raise InvalidPriceRangeError("from_date must be <= to_date")

    instrument = db.get(Instrument, isin)
    if instrument is None:
        raise InstrumentNotFoundError(f"Instrument not found: {isin}")
    if not instrument.ticker_symbol:
        raise TickerNotConfiguredError(
            "Instrument has no ticker_symbol; PATCH it first via "
            "/portfolio/instruments/{isin}"
        )

    try:
        points = provider.get_history(
            instrument.ticker_symbol,
            from_date,
            to_date,
            expected_currency=instrument.currency,
        )
    except CurrencyMismatchError as exc:
        logger.warning(
            "backfill-prices(%s, ticker=%s) currency mismatch: %s",
            isin,
            instrument.ticker_symbol,
            exc,
        )
        # Currency mismatches are user-actionable (wrong ticker) — surface
        # the upstream message verbatim so the UI can show "EUR expected,
        # USD found" or similar.
        raise CurrencyMismatchProviderError(str(exc)) from exc
    except PriceFetchError as exc:
        logger.warning(
            "backfill-prices(%s, ticker=%s) provider error: %s",
            isin,
            instrument.ticker_symbol,
            exc,
        )
        raise PriceProviderError("Upstream price provider failed") from exc

    if not points:
        return BackfillResult(
            isin=isin,
            ticker_symbol=instrument.ticker_symbol,
            requested_from=from_date,
            requested_to=to_date,
            fetched_points=0,
            inserted_points=0,
            skipped_existing=0,
        )

    existing_dates = set(
        db.execute(
            select(InstrumentPriceHistory.price_date).where(
                InstrumentPriceHistory.isin == isin,
                InstrumentPriceHistory.price_date >= from_date,
                InstrumentPriceHistory.price_date <= to_date,
            )
        )
        .scalars()
        .all()
    )

    inserted = 0
    skipped = 0
    for point in points:
        if point.price_date in existing_dates:
            skipped += 1
            continue
        db.add(
            InstrumentPriceHistory(
                isin=isin,
                price_date=point.price_date,
                close=point.close,
                currency=point.currency or instrument.currency,
                source=PRICE_SOURCE,
            )
        )
        existing_dates.add(point.price_date)
        inserted += 1
    db.commit()

    return BackfillResult(
        isin=isin,
        ticker_symbol=instrument.ticker_symbol,
        requested_from=from_date,
        requested_to=to_date,
        fetched_points=len(points),
        inserted_points=inserted,
        skipped_existing=skipped,
    )
