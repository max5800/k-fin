"""Yahoo Finance market-history client.

External (non-bank) price provider used to backfill historical close
prices for portfolio instruments. Comdirect returns only the booked
value at each transaction; for an instrument-level performance chart
we need a daily price series the user can request on demand.

This module wraps `yfinance` behind a small, mockable surface:

* :func:`get_history` — fetch one ticker's split- and dividend-adjusted
  close series in a date range. We pass ``auto_adjust=True`` so a
  stock split doesn't synthetically halve the chart on the day it
  happens. Adjusted closes are appropriate for performance views; the
  bilanzielle Bewertung kommt aus Comdirect-Buchungen, nicht hieraus.

The wrapper deliberately does not maintain its own cache or persist
anything — that is the responsibility of the caller (see
``src/api/routers/portfolio.py``'s backfill endpoint, which writes
into ``instrument_price_history``). yfinance's own response can be a
silently empty DataFrame, a pandas error, or a network error;
everything is normalised here to either a list of :class:`PricePoint`,
:class:`PriceFetchError`, or :class:`CurrencyMismatchError`.

Currency-mismatch handling: yfinance returns whatever currency the
ticker trades in (``AAPL`` → USD, ``SAP.DE`` → EUR). If the caller
mistakenly maps a USD ticker to a EUR instrument, naive insertion
into ``instrument_price_history`` would silently mix currencies in
the chart. ``get_history`` accepts an ``expected_currency`` and
raises :class:`CurrencyMismatchError` when the provider disagrees;
the caller surfaces this as a 4xx and tells the user to re-map the
ticker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from src.core.logging import get_logger

logger = get_logger("yfinance")


@dataclass(frozen=True)
class PricePoint:
    """One daily price observation."""

    price_date: date
    close: Decimal
    currency: str


class PriceFetchError(RuntimeError):
    """Raised when yfinance returned no usable data or failed outright.

    Wraps both the empty-DataFrame case (yfinance loves to swallow
    rate-limits this way) and any pandas / network exception.
    """


class CurrencyMismatchError(RuntimeError):
    """Raised when the provider's currency for a ticker disagrees with the
    instrument's recorded currency.

    Caller is expected to bubble this up as a 4xx — silently writing a
    USD close into a EUR instrument's history would corrupt every
    chart that touches the row.
    """


class HistoryProvider(Protocol):
    """Minimal interface so the API layer can mock the provider in tests."""

    def get_history(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
        expected_currency: str | None = None,
    ) -> list[PricePoint]: ...


class YFinanceClient:
    """Functional wrapper around yfinance.Ticker.history().

    Kept as a class (rather than a free function) so it can be swapped
    via FastAPI dependency override in tests without monkey-patching the
    yfinance module globally.
    """

    def get_history(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
        expected_currency: str | None = None,
    ) -> list[PricePoint]:
        """Fetch daily split/dividend-adjusted close prices for ``ticker``.

        Args:
            ticker: Yahoo-Finance ticker symbol (e.g. ``SAP.DE``).
            from_date: Inclusive start date.
            to_date: Inclusive end date.
            expected_currency: ISO code (``"EUR"``, ``"USD"``) of the
                instrument the caller is going to write these prices
                into. If supplied and the provider disagrees, raises
                :class:`CurrencyMismatchError` *before* any data is
                returned — protects ``instrument_price_history`` from
                quietly accumulating mixed-currency closes.

        Returns:
            List of :class:`PricePoint`, ordered by date ascending.
            May be empty if yfinance has no observations in the window
            (e.g. weekends only, future dates) — the caller decides how
            to surface that.

        Raises:
            PriceFetchError: yfinance threw or rate-limited.
            CurrencyMismatchError: ``expected_currency`` was supplied
                and the provider returned a different currency.
        """
        if from_date > to_date:
            raise ValueError(
                f"from_date ({from_date}) must be <= to_date ({to_date})"
            )
        if not ticker:
            raise ValueError("ticker must be a non-empty string")

        # yfinance treats `end` as exclusive; nudge by +1 day so the
        # caller's inclusive range covers the actual to_date close.
        end_param = date.fromordinal(to_date.toordinal() + 1)

        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover — install-time guard
            raise PriceFetchError(
                "yfinance is not installed; run `uv sync`"
            ) from exc

        try:
            t = yf.Ticker(ticker)
            df = t.history(
                start=from_date.isoformat(),
                end=end_param.isoformat(),
                # Adjusted closes — splits and dividends are folded in,
                # so a 4-for-1 split day doesn't show up as a synthetic
                # 75 % drop in the chart. Bilanzielle Bewertung lebt im
                # Comdirect-Buchungspfad, nicht hier.
                auto_adjust=True,
                actions=False,
                raise_errors=False,
            )
        except Exception as exc:  # yfinance raises a grab-bag of types
            logger.warning(
                "yfinance.history(%s, %s..%s) raised: %s",
                ticker,
                from_date,
                to_date,
                exc,
            )
            raise PriceFetchError(
                f"yfinance fetch failed for {ticker}: {exc}"
            ) from exc

        if df is None or df.empty:
            logger.info(
                "yfinance.history(%s, %s..%s) returned empty frame",
                ticker,
                from_date,
                to_date,
            )
            return []

        currency = "EUR"
        try:
            info_currency = (t.fast_info or {}).get("currency")
            if info_currency:
                currency = str(info_currency).upper()
        except Exception:
            # fast_info is best-effort; not worth failing the call over.
            pass

        # Validate currency against the caller's expectation *before* we
        # build any PricePoints — a mismatch invalidates the entire batch.
        if expected_currency is not None:
            expected_norm = expected_currency.strip().upper()
            if expected_norm and currency != expected_norm:
                raise CurrencyMismatchError(
                    f"yfinance returns {currency} for ticker {ticker}, "
                    f"instrument is in {expected_norm}"
                )

        out: list[PricePoint] = []
        for ts, row in df.iterrows():
            close = row.get("Close")
            if close is None:
                continue
            try:
                close_dec = Decimal(str(close))
            except (ValueError, ArithmeticError):
                continue
            row_date = ts.date() if hasattr(ts, "date") else ts
            out.append(
                PricePoint(price_date=row_date, close=close_dec, currency=currency)
            )

        out.sort(key=lambda p: p.price_date)
        logger.info(
            "yfinance.history(%s, %s..%s) -> %d points (currency=%s)",
            ticker,
            from_date,
            to_date,
            len(out),
            currency,
        )
        return out
