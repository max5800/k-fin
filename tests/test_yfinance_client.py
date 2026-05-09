"""Unit tests for the yfinance history wrapper.

We don't actually hit Yahoo Finance — we mock ``yfinance.Ticker`` so
the returned ``history()`` DataFrame can be controlled deterministically.
This covers the three behaviours that bit us in manual exploration:
empty frames (silent rate-limit), exceptions (network errors), and the
inclusive end-date contract.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.connector.yfinance_client import (
    CurrencyMismatchError,
    PriceFetchError,
    PricePoint,
    YFinanceClient,
)


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a DataFrame shaped like yfinance's `Ticker.history()` output."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"Close": [r[1] for r in rows]}, index=idx)


@pytest.fixture
def fake_yf():
    """Patch the yfinance module so no live request happens."""
    fake_module = MagicMock()
    with patch.dict(sys.modules, {"yfinance": fake_module}):
        yield fake_module


def test_get_history_returns_sorted_price_points(fake_yf):
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: _frame(
            [("2026-04-02", 101.5), ("2026-04-01", 100.0), ("2026-04-03", 102.25)]
        ),
        fast_info={"currency": "EUR"},
    )

    client = YFinanceClient()
    points = client.get_history("SAP.DE", date(2026, 4, 1), date(2026, 4, 3))

    assert len(points) == 3
    assert all(isinstance(p, PricePoint) for p in points)
    # Ascending by date
    assert [p.price_date for p in points] == [
        date(2026, 4, 1),
        date(2026, 4, 2),
        date(2026, 4, 3),
    ]
    assert points[0].close == Decimal("100.0")
    assert points[1].currency == "EUR"


def test_get_history_inclusive_end_date_passes_exclusive_to_yfinance(fake_yf):
    captured = {}

    def fake_history(**kw):
        captured.update(kw)
        return _frame([("2026-04-01", 100.0)])

    fake_yf.Ticker.return_value = SimpleNamespace(
        history=fake_history, fast_info={"currency": "USD"}
    )

    YFinanceClient().get_history("AAPL", date(2026, 4, 1), date(2026, 4, 5))

    # yfinance's `end` is exclusive; we must pass to_date + 1
    assert captured["start"] == "2026-04-01"
    assert captured["end"] == "2026-04-06"


def test_get_history_empty_frame_returns_empty_list(fake_yf):
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: pd.DataFrame(),
        fast_info={"currency": "EUR"},
    )

    points = YFinanceClient().get_history(
        "BOGUS.DE", date(2026, 4, 1), date(2026, 4, 5)
    )
    assert points == []


def test_get_history_none_frame_returns_empty_list(fake_yf):
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: None,
        fast_info={},
    )

    assert (
        YFinanceClient().get_history("X", date(2026, 4, 1), date(2026, 4, 1)) == []
    )


def test_get_history_wraps_exception_as_price_fetch_error(fake_yf):
    def boom(**kw):
        raise RuntimeError("rate limited")

    fake_yf.Ticker.return_value = SimpleNamespace(
        history=boom, fast_info={"currency": "EUR"}
    )

    with pytest.raises(PriceFetchError, match="rate limited"):
        YFinanceClient().get_history("X", date(2026, 4, 1), date(2026, 4, 5))


def test_get_history_uses_currency_fallback_when_fast_info_blows_up(fake_yf):
    class Tk:
        history = staticmethod(lambda **kw: _frame([("2026-04-01", 50.0)]))

        @property
        def fast_info(self):
            raise RuntimeError("yfinance internal")

    fake_yf.Ticker.return_value = Tk()

    points = YFinanceClient().get_history("X", date(2026, 4, 1), date(2026, 4, 1))
    assert len(points) == 1
    # default fallback currency
    assert points[0].currency == "EUR"


def test_get_history_validates_date_order(fake_yf):
    with pytest.raises(ValueError):
        YFinanceClient().get_history("X", date(2026, 4, 5), date(2026, 4, 1))


def test_get_history_rejects_blank_ticker(fake_yf):
    with pytest.raises(ValueError):
        YFinanceClient().get_history("", date(2026, 4, 1), date(2026, 4, 5))


# ---------------------------------------------------------------------------
# F2 — auto_adjust=True (split- and dividend-adjusted closes)
# ---------------------------------------------------------------------------


def test_get_history_uses_auto_adjust_true(fake_yf):
    """Stock splits would otherwise show up as synthetic price drops on
    the split day. Adjusted closes fold splits and dividends back in,
    matching what the user would see on a broker chart.
    """
    captured = {}

    def fake_history(**kw):
        captured.update(kw)
        return _frame([("2026-04-01", 100.0)])

    fake_yf.Ticker.return_value = SimpleNamespace(
        history=fake_history, fast_info={"currency": "EUR"}
    )

    YFinanceClient().get_history("SAP.DE", date(2026, 4, 1), date(2026, 4, 1))
    # The whole point of F2: do NOT pass `auto_adjust=False`. Pin it.
    assert captured["auto_adjust"] is True


# ---------------------------------------------------------------------------
# F1 — Currency mismatch protection
# ---------------------------------------------------------------------------


def test_get_history_raises_on_currency_mismatch(fake_yf):
    """yfinance returns USD for AAPL; the instrument is recorded as EUR.
    Writing the USD closes into the EUR instrument's price history would
    silently corrupt every chart that touches the row — must raise
    instead.
    """
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: _frame([("2026-04-01", 175.0)]),
        fast_info={"currency": "USD"},
    )

    client = YFinanceClient()
    with pytest.raises(CurrencyMismatchError) as excinfo:
        client.get_history(
            "AAPL", date(2026, 4, 1), date(2026, 4, 1), expected_currency="EUR"
        )
    msg = str(excinfo.value)
    assert "USD" in msg
    assert "EUR" in msg
    assert "AAPL" in msg


def test_get_history_currency_match_returns_points(fake_yf):
    """Provider currency == expected currency: pass through unchanged."""
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: _frame([("2026-04-01", 100.0)]),
        fast_info={"currency": "EUR"},
    )

    points = YFinanceClient().get_history(
        "SAP.DE", date(2026, 4, 1), date(2026, 4, 1), expected_currency="EUR"
    )
    assert len(points) == 1
    assert points[0].currency == "EUR"


def test_get_history_currency_check_normalizes_case(fake_yf):
    """A caller passing lowercase ``"eur"`` shouldn't trip the check."""
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: _frame([("2026-04-01", 100.0)]),
        fast_info={"currency": "EUR"},
    )

    points = YFinanceClient().get_history(
        "SAP.DE", date(2026, 4, 1), date(2026, 4, 1), expected_currency="eur"
    )
    assert len(points) == 1


def test_get_history_currency_check_skipped_when_expected_is_none(fake_yf):
    """Backward-compat: callers that don't pass ``expected_currency``
    (yet) keep the old behaviour — the provider's currency wins."""
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: _frame([("2026-04-01", 175.0)]),
        fast_info={"currency": "USD"},
    )

    points = YFinanceClient().get_history(
        "AAPL", date(2026, 4, 1), date(2026, 4, 1)
    )
    assert points[0].currency == "USD"


def test_get_history_currency_mismatch_raised_before_dataframe_read(fake_yf):
    """Mismatch must short-circuit *before* PricePoints are built — the
    caller never sees partial data they could mistakenly write."""
    rows_iterated = {"count": 0}

    class CountingDF:
        empty = False

        def iterrows(self):
            rows_iterated["count"] += 1
            yield from [("ts", {"Close": 100.0})]

    # Wrap the real frame so we can detect iteration. We can't easily
    # subclass DataFrame, so use a fake object with the bits get_history
    # touches: `is None` → False, `.empty` → False, `.iterrows()` → counts.
    fake_yf.Ticker.return_value = SimpleNamespace(
        history=lambda **kw: CountingDF(),
        fast_info={"currency": "USD"},
    )

    with pytest.raises(CurrencyMismatchError):
        YFinanceClient().get_history(
            "AAPL", date(2026, 4, 1), date(2026, 4, 1), expected_currency="EUR"
        )
    assert rows_iterated["count"] == 0
