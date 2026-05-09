"""Validation caps for query/body parameters.

These guard the API against accidental and adversarial unbounded inputs:
  - ``GET /transactions?tag_ids=...`` would otherwise expand into an
    unbounded ``IN (...)`` SQL list (10k tags = planner pain).
  - ``POST .../backfill-prices`` would otherwise accept arbitrary date
    spans and pin the worker / risk a yfinance-IP-ban on 100-year windows.

Both fixes live close to the schema/router boundary so every caller
benefits without each endpoint having to remember the cap.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.routers.transactions import _MAX_TAG_IDS, _apply_filters
from src.api.schemas import PriceBackfillRequest
from src.core.db.models import NormalizedTransaction


@pytest.fixture
def api_client(db_engine):
    """Minimal FastAPI client backed by the test Postgres engine.

    Used by the HTTP-level cap tests below — keeps each test file
    self-contained instead of cross-importing from test_finance_api.
    """
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(os.environ, {"API_TOKEN": "test-secret", "DATABASE_URL": db_url}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# tag_ids cap (M1)
# ---------------------------------------------------------------------------


def _filter_kwargs(**overrides):
    """Default-everything-None filter kwargs so individual tests only have
    to set the field under exam."""
    base = dict(
        date_from=None,
        date_to=None,
        category_id=None,
        tag_ids=None,
        is_recurring=None,
        is_outlier=None,
        internal_transfer=None,
        is_refund=None,
        search=None,
    )
    base.update(overrides)
    return base


def test_tag_ids_at_cap_is_accepted():
    tag_ids = [f"tag-{i}" for i in range(_MAX_TAG_IDS)]
    # Should not raise — exactly at the cap is fine.
    _apply_filters(select(NormalizedTransaction), **_filter_kwargs(tag_ids=tag_ids))


def test_tag_ids_above_cap_raises_400():
    tag_ids = [f"tag-{i}" for i in range(_MAX_TAG_IDS + 1)]
    with pytest.raises(HTTPException) as excinfo:
        _apply_filters(
            select(NormalizedTransaction), **_filter_kwargs(tag_ids=tag_ids)
        )
    assert excinfo.value.status_code == 400
    assert str(_MAX_TAG_IDS) in excinfo.value.detail


def test_tag_ids_far_above_cap_does_not_reach_db():
    """If the validation fires before SQL is built, even a 10k payload is
    cheap. This pins the perf-guard intent of the cap.
    """
    tag_ids = [f"tag-{i}" for i in range(10_000)]
    with pytest.raises(HTTPException):
        _apply_filters(
            select(NormalizedTransaction), **_filter_kwargs(tag_ids=tag_ids)
        )


def test_tag_ids_none_is_unaffected():
    _apply_filters(select(NormalizedTransaction), **_filter_kwargs(tag_ids=None))


def test_tag_ids_empty_list_is_unaffected():
    _apply_filters(select(NormalizedTransaction), **_filter_kwargs(tag_ids=[]))


def test_tag_ids_cap_returns_400_via_http(api_client):
    """End-to-end: an oversized tag_ids query string from the public API
    surface returns HTTP 400, not a 500 / runaway query."""
    too_many = "&".join(f"tag_ids=t{i}" for i in range(_MAX_TAG_IDS + 5))
    resp = api_client.get(
        f"/api/v1/transactions?{too_many}",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 400
    assert str(_MAX_TAG_IDS) in resp.json()["detail"]


def test_tag_ids_export_endpoint_also_capped(api_client):
    """The /transactions/export endpoint shares ``_apply_filters`` and
    therefore inherits the cap. Pin that contract — a CSV export with
    10k tag IDs would be a much fatter DoS than the JSON list."""
    too_many = "&".join(f"tag_ids=t{i}" for i in range(_MAX_TAG_IDS + 5))
    resp = api_client.get(
        f"/api/v1/transactions/export?{too_many}",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PriceBackfillRequest window cap (H3)
# ---------------------------------------------------------------------------


def test_backfill_request_accepts_one_year():
    req = PriceBackfillRequest(
        from_date=date(2025, 1, 1), to_date=date(2026, 1, 1)
    )
    assert (req.to_date - req.from_date).days == 365


def test_backfill_request_accepts_five_years_minus_one():
    """4 years 364 days — well under the 5-year cap."""
    PriceBackfillRequest(
        from_date=date(2021, 1, 2), to_date=date(2026, 1, 1)
    )


def test_backfill_request_rejects_six_years():
    with pytest.raises(ValidationError) as excinfo:
        PriceBackfillRequest(
            from_date=date(2020, 1, 1), to_date=date(2026, 1, 1)
        )
    assert "5 years" in str(excinfo.value)


def test_backfill_request_rejects_century_window():
    """The original H3 case: 1900-2030 must not slip through and pin the
    worker on a yfinance call."""
    with pytest.raises(ValidationError):
        PriceBackfillRequest(
            from_date=date(1900, 1, 1), to_date=date(2030, 1, 1)
        )


def test_backfill_request_inverted_range_is_routers_problem():
    """Schema only caps the *width* of the window; the *order* check is
    enforced by the router (HTTP 400). An inverted range slips through
    pydantic so the existing router behaviour stays intact.
    """
    # Should construct without error — the router converts this to 400.
    PriceBackfillRequest(
        from_date=date(2026, 4, 10), to_date=date(2026, 4, 1)
    )


def test_backfill_request_accepts_same_day():
    req = PriceBackfillRequest(
        from_date=date(2026, 4, 1), to_date=date(2026, 4, 1)
    )
    assert req.to_date - req.from_date == timedelta(0)
