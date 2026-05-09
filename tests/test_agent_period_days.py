"""Unit tests for the `period_days` override on the period-aware agents.

Each agent (weekly, monthly, anomaly) honours a `period_days` override
that replaces the agent's built-in window. The tests below short-circuit
the LLM by stubbing the data-gathering helpers to return empty lists —
that path returns a deterministic result without touching any model,
which is enough to verify the date arithmetic without spending tokens.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch


class TestWeeklyPeriodDays:
    def test_default_window_is_current_iso_week(self):
        from src.agents import weekly_analysis as mod

        captured: dict = {}

        def fake_get_period(_engine, start, end):
            captured["start"] = start
            captured["end"] = end
            return []

        with patch.object(mod, "get_period_transactions", fake_get_period):
            result = mod.run_weekly_analysis(
                engine=None, reference_date=date(2026, 5, 6)  # Wednesday
            )
        # ISO week of 2026-05-06 starts Mon 2026-05-04, ends Sun 2026-05-10.
        assert captured["start"] == date(2026, 5, 4)
        assert captured["end"] == date(2026, 5, 10)
        assert result.period == "2026-W19"

    def test_period_days_overrides_window(self):
        from src.agents import weekly_analysis as mod

        captured: dict = {}

        def fake_get_period(_engine, start, end):
            captured["start"] = start
            captured["end"] = end
            return []

        with patch.object(mod, "get_period_transactions", fake_get_period):
            mod.run_weekly_analysis(
                engine=None,
                reference_date=date(2026, 5, 6),
                period_days=14,
            )
        # 14-day window ending on the reference date.
        assert captured["end"] == date(2026, 5, 6)
        assert captured["start"] == date(2026, 5, 6) - timedelta(days=13)


class TestMonthlyPeriodDays:
    def test_default_window_is_previous_month(self):
        from src.agents import monthly_analysis as mod

        captured: dict = {}

        def fake_get_period(_engine, start, end):
            captured["start"] = start
            captured["end"] = end
            return []

        with patch.object(mod, "get_period_transactions", fake_get_period):
            result = mod.run_monthly_analysis(
                engine=None, reference_date=date(2026, 5, 6)
            )
        assert captured["start"] == date(2026, 4, 1)
        assert captured["end"] == date(2026, 4, 30)
        assert result.period == "2026-04"

    def test_period_days_overrides_window_and_uses_iso_range_label(self):
        from src.agents import monthly_analysis as mod

        captured: dict = {}

        def fake_get_period(_engine, start, end):
            captured["start"] = start
            captured["end"] = end
            return []

        with patch.object(mod, "get_period_transactions", fake_get_period):
            result = mod.run_monthly_analysis(
                engine=None,
                reference_date=date(2026, 5, 6),
                period_days=90,
            )
        assert captured["end"] == date(2026, 5, 6)
        assert captured["start"] == date(2026, 5, 6) - timedelta(days=89)
        # Non-month windows fall back to the ISO date-range label so the
        # report key stays distinct from the regular monthly slot.
        assert result.period == "2026-02-06/2026-05-06"


class TestAnomalyPeriodDays:
    def test_default_lookback_is_30_days(self):
        from src.agents import anomaly as mod

        captured: dict = {"date_from": None, "ref": None}

        def fake_get_outliers(_engine, date_from, ref):
            captured["date_from"] = date_from
            captured["ref"] = ref
            return []

        with (
            patch.object(mod, "get_outlier_transactions", fake_get_outliers),
            patch.object(mod, "get_new_counterparties", lambda *a, **kw: []),
            patch.object(mod, "get_period_transactions", lambda *a, **kw: []),
        ):
            mod.run_anomaly_detection(
                engine=None, reference_date=date(2026, 5, 6)
            )
        assert captured["ref"] == date(2026, 5, 6)
        assert captured["date_from"] == date(2026, 5, 6) - timedelta(days=30)

    def test_period_days_overrides_lookback_days(self):
        from src.agents import anomaly as mod

        captured: dict = {}

        def fake_get_outliers(_engine, date_from, ref):
            captured["date_from"] = date_from
            captured["ref"] = ref
            return []

        with (
            patch.object(mod, "get_outlier_transactions", fake_get_outliers),
            patch.object(mod, "get_new_counterparties", lambda *a, **kw: []),
            patch.object(mod, "get_period_transactions", lambda *a, **kw: []),
        ):
            mod.run_anomaly_detection(
                engine=None,
                reference_date=date(2026, 5, 6),
                lookback_days=30,
                period_days=7,
            )
        # period_days wins over lookback_days.
        assert captured["date_from"] == date(2026, 5, 6) - timedelta(days=7)
