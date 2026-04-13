"""Unit tests for the pure-function flag helpers.

Integration tests against a real Postgres instance live in
test_ground_truth.py with a testcontainers-based fixture.
"""

from decimal import Decimal

import pandas as pd
import pytest

from src.normalization.pipeline import NormalizationPipeline


@pytest.fixture
def pipeline():
    return NormalizationPipeline("sqlite:///:memory:")


def _row(**overrides):
    base = {
        "id": "",
        "amount": Decimal("0"),
        "booking_date": pd.to_datetime("2026-01-01").date(),
        "recipient": None,
        "sender": None,
        "sender_iban": None,
        "recipient_iban": None,
        "category_id": None,
        "internal_transfer": False,
        "is_recurring": False,
        "is_outlier": False,
    }
    base.update(overrides)
    return base


def test_flag_internal_transfers_matches_cents_bucket(pipeline):
    df = pd.DataFrame([
        _row(id="1", amount=Decimal("100.00"), sender_iban="DE01", recipient_iban="DE02"),
        _row(id="2", amount=Decimal("-100.00"), sender_iban="DE02", recipient_iban="DE01"),
        _row(id="3", amount=Decimal("50.00"), sender_iban="DE99", recipient_iban="DE02"),
    ])
    own = {"DE01", "DE02"}
    result = pipeline._flag_internal_transfers(df, own_ibans=own)
    flagged = set(result.loc[result["internal_transfer"], "id"])
    assert flagged == {"1", "2"}


def test_flag_internal_transfers_requires_own_ibans_on_both_sides(pipeline):
    df = pd.DataFrame([
        _row(id="1", amount=Decimal("100.00"), sender_iban="DE01", recipient_iban="DE02"),
        _row(id="2", amount=Decimal("-100.00"), sender_iban="DE99", recipient_iban="DEEXT"),
    ])
    own = {"DE01", "DE02"}
    result = pipeline._flag_internal_transfers(df, own_ibans=own)
    assert not result["internal_transfer"].any()


def test_flag_recurring_requires_consecutive_months(pipeline):
    # Jan, Feb, Apr (gap in March) — NOT recurring per 2026-04-12 decision
    df = pd.DataFrame([
        _row(id="1", recipient="Netflix", amount=Decimal("-14.99"),
             booking_date=pd.to_datetime("2026-01-15").date()),
        _row(id="2", recipient="Netflix", amount=Decimal("-14.99"),
             booking_date=pd.to_datetime("2026-02-15").date()),
        _row(id="3", recipient="Netflix", amount=Decimal("-14.99"),
             booking_date=pd.to_datetime("2026-04-15").date()),
    ])
    result, patterns = pipeline._flag_recurring(df)
    assert not result["is_recurring"].any()
    assert patterns == []


def test_flag_recurring_three_consecutive_months(pipeline):
    df = pd.DataFrame([
        _row(id="1", recipient="Netflix", amount=Decimal("-14.99"),
             booking_date=pd.to_datetime("2026-01-15").date()),
        _row(id="2", recipient="Netflix", amount=Decimal("-14.99"),
             booking_date=pd.to_datetime("2026-02-15").date()),
        _row(id="3", recipient="Netflix", amount=Decimal("-14.99"),
             booking_date=pd.to_datetime("2026-03-15").date()),
        _row(id="4", recipient="OneTimeFix", amount=Decimal("-100.00"),
             booking_date=pd.to_datetime("2026-01-15").date()),
    ])
    result, patterns = pipeline._flag_recurring(df)
    flagged = set(result.loc[result["is_recurring"], "id"])
    assert flagged == {"1", "2", "3"}
    assert len(patterns) == 1
    assert patterns[0]["recipient"] == "Netflix"
    assert patterns[0]["occurrence_count"] == 3


def test_flag_recurring_rejects_amount_outside_tolerance(pipeline):
    # ±10% tolerance on mean ≈ 50 → amounts 10 / 50 / 90 exceed tolerance
    df = pd.DataFrame([
        _row(id="1", recipient="VariableBill", amount=Decimal("-10.00"),
             booking_date=pd.to_datetime("2026-01-15").date()),
        _row(id="2", recipient="VariableBill", amount=Decimal("-50.00"),
             booking_date=pd.to_datetime("2026-02-15").date()),
        _row(id="3", recipient="VariableBill", amount=Decimal("-90.00"),
             booking_date=pd.to_datetime("2026-03-15").date()),
    ])
    result, patterns = pipeline._flag_recurring(df)
    assert not result["is_recurring"].any()
    assert patterns == []


def test_flag_outliers_zscore_per_category(pipeline):
    amounts = [10, 12, 9, 11, 10.5, 9.5, 11.5, 10.2, 11.8, 9.8, 1000]
    df = pd.DataFrame([
        _row(id=str(i), category_id="CatA", amount=Decimal(str(a)))
        for i, a in enumerate(amounts)
    ])
    result = pipeline._flag_outliers(df)
    outliers = result.loc[result["is_outlier"]]
    assert len(outliers) == 1
    assert outliers.iloc[0]["amount"] == Decimal("1000")
