"""Unit tests for `derive_period_label`.

The function is the single source of truth for the (label, start, end)
triple every period-aware agent emits. These tests pin down both the
default semantics per agent kind and the override path that switches
all kinds to the canonical date-range label.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.agents.period import derive_period_label


class TestDefaultSemantics:
    def test_weekly_default_returns_iso_week_label(self):
        # 2026-05-06 is a Wednesday; ISO week starts Mon 2026-05-04.
        label, start, end = derive_period_label("weekly", None, date(2026, 5, 6))
        assert label == "2026-W19"
        assert start == date(2026, 5, 4)
        assert end == date(2026, 5, 10)

    def test_weekly_default_on_monday_keeps_same_week(self):
        label, start, end = derive_period_label("weekly", None, date(2026, 5, 4))
        assert label == "2026-W19"
        assert start == date(2026, 5, 4)
        assert end == date(2026, 5, 10)

    def test_monthly_default_returns_previous_complete_month(self):
        label, start, end = derive_period_label("monthly", None, date(2026, 5, 6))
        assert label == "2026-04"
        assert start == date(2026, 4, 1)
        assert end == date(2026, 4, 30)

    def test_monthly_default_on_first_of_month_picks_prior_month(self):
        # Edge case: ref is the first of the month → previous month.
        label, start, end = derive_period_label("monthly", None, date(2026, 1, 1))
        assert label == "2025-12"
        assert start == date(2025, 12, 1)
        assert end == date(2025, 12, 31)

    def test_anomaly_default_emits_iso_date_range(self):
        label, start, end = derive_period_label("anomaly", None, date(2026, 5, 6))
        assert label == "2026-04-06/2026-05-06"
        assert end == date(2026, 5, 6)
        assert start == date(2026, 5, 6) - timedelta(days=30)


class TestOverrideSemantics:
    @pytest.mark.parametrize(
        "kind,days,expected_start_offset",
        [
            # weekly/monthly use N-day inclusive: start = ref - (days - 1)
            ("weekly", 14, 13),
            ("weekly", 1, 0),
            ("monthly", 90, 89),
            # anomaly preserves the legacy `lookback_days` offset
            ("anomaly", 7, 7),
            ("anomaly", 60, 60),
        ],
    )
    def test_override_returns_iso_date_range_for_all_kinds(
        self, kind, days, expected_start_offset
    ):
        ref = date(2026, 5, 6)
        label, start, end = derive_period_label(kind, days, ref)
        assert end == ref
        assert start == ref - timedelta(days=expected_start_offset)
        # Override always switches to canonical date-range form.
        assert label == f"{start.isoformat()}/{end.isoformat()}"

    def test_override_label_distinct_from_iso_week_form(self):
        # Same 7-day window as the default ISO week, but override path
        # MUST emit the date-range form so report keys stay distinct.
        ref = date(2026, 5, 10)  # Sunday
        label, _, _ = derive_period_label("weekly", 7, ref)
        assert "/" in label
        assert "W" not in label

    def test_zero_or_negative_days_treated_as_default(self):
        # Defensive: `days <= 0` should fall through to the default.
        ref = date(2026, 5, 6)
        label_zero, _, _ = derive_period_label("weekly", 0, ref)
        label_neg, _, _ = derive_period_label("weekly", -3, ref)
        label_default, _, _ = derive_period_label("weekly", None, ref)
        assert label_zero == label_default
        assert label_neg == label_default
