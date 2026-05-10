"""Integration tests for the M11 instrument-price endpoints.

The api router has been split: PATCH ticker mutates Postgres in-process
(user-only auth), POST backfill-prices forwards to the worker
(``POST /internal/portfolio/backfill-prices``). GET prices stays
in-process, read-only.

Two harnesses cover both halves:

* ``api_client`` — boots the api app and mocks the httpx call out to the
  worker so we can verify the dispatcher forwards correctly and maps
  worker-side errors to the right HTTP statuses.
* ``worker_client`` — boots the worker app (``main:app``) with a fake
  yfinance provider injected via ``dependency_overrides`` so we can
  verify the actual DB write path and yfinance error handling.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.external.yfinance_client import (
    PriceFetchError,
    PricePoint,
)
from src.core.db.models import (
    Depot,
    Instrument,
    InstrumentPriceHistory,
    Position,
)

AUTH = {"Authorization": "Bearer test-secret"}

# Use a letter-tagged fake ISIN so gitleaks' \bDE\d{20}\b rule does not fire.
FAKE_ISIN = "DE0000000000"


# ---------------------------------------------------------------------------
# Worker-side fake provider + harness
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal HistoryProvider fake driven by per-test setup."""

    def __init__(
        self,
        points: list[PricePoint] | None = None,
        error: Exception | None = None,
    ):
        self.points = points or []
        self.error = error
        self.calls: list[tuple[str, date, date]] = []

    def get_history(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
        *,
        expected_currency: str | None = None,
    ) -> list[PricePoint]:
        self.calls.append((ticker, from_date, to_date))
        if self.error is not None:
            raise self.error
        return [
            p for p in self.points if from_date <= p.price_date <= to_date
        ]


@pytest.fixture
def fake_provider():
    return FakeProvider()


@pytest.fixture
def worker_client(db_engine, fake_provider):
    """TestClient bound to ``main:app`` (worker) with the yfinance
    provider replaced by ``fake_provider`` and DATABASE_URL pointed at
    the testcontainer.
    """
    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(
        os.environ,
        {"DATABASE_URL": db_url, "API_TOKEN": "test-secret"},
    ):
        # Refresh the shared settings singleton so the worker route
        # picks up the patched DATABASE_URL.
        from src.core.config import Settings, settings as _settings

        for k, v in Settings().model_dump().items():
            setattr(_settings, k, v)

        import main as worker_main

        worker_main.app.dependency_overrides[
            worker_main._get_history_provider
        ] = lambda: fake_provider
        # The new get_engine dep reads from app.state.engine. The
        # TestClient skips lifespan unless used as a context manager,
        # so install the testcontainer engine ourselves.
        worker_main.set_test_engine(db_engine)
        client = TestClient(worker_main.app)
        try:
            yield client
        finally:
            worker_main.app.dependency_overrides.clear()
            if hasattr(worker_main.app.state, "engine"):
                del worker_main.app.state.engine


# ---------------------------------------------------------------------------
# API-side harness — mocks httpx so we never actually hit the worker.
# ---------------------------------------------------------------------------


class FakeHttpxResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (str(json_body) if json_body else "")

    def json(self):
        if self._json is None:
            raise ValueError("No JSON body")
        return self._json


@pytest.fixture
def httpx_post_spy():
    """Patch ``src.api.routers.portfolio.httpx.post`` and record calls.

    Tests set ``.return_value`` (FakeHttpxResponse) or ``.side_effect``
    (Exception or callable) to control what the dispatcher sees.
    """
    calls: list[dict] = []

    class Spy:
        def __init__(self):
            self.return_value: FakeHttpxResponse | None = None
            self.side_effect = None

        def __call__(self, url, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})
            if self.side_effect is not None:
                if isinstance(self.side_effect, Exception):
                    raise self.side_effect
                return self.side_effect(url, json=json, timeout=timeout)
            assert self.return_value is not None, "test forgot to set return_value"
            return self.return_value

    spy = Spy()
    spy.calls = calls

    with patch("src.api.routers.portfolio.httpx.post", spy):
        yield spy


@pytest.fixture
def api_client(db_engine):
    """Spin up the api FastAPI app with a TestClient.

    Sets API_TOKEN + DATABASE_URL + JWT_SECRET in env before
    ``create_app()`` so the Settings refresh has all three available,
    then walks every still-loaded ``settings`` instance and forces
    the same values into them. The latter step defends against test
    pollution from ``tests/auth/test_jwt.py``, which does
    ``importlib.reload(src.core.config)`` and leaves divergent
    Settings singletons across modules — without re-syncing them,
    the request handler in ``src.api.deps`` and ``src.api.auth.jwt``
    can read different ``jwt_secret`` values for the same request.
    """
    import sys

    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    api_token = "test-secret"
    jwt_secret = "test-jwt-secret-min-32chars-long-aaaaaaaa"
    env = {
        "API_TOKEN": api_token,
        "DATABASE_URL": db_url,
        "JWT_SECRET": jwt_secret,
    }
    with patch.dict(os.environ, env):
        from src.api.app import create_app
        from src.core.config import Settings as _SettingsCls

        app = create_app()

        # Sync every loaded module's `settings` instance — needed
        # because some prior test may have done an importlib.reload
        # of `src.core.config`, leaving stale references that the
        # production import graph still uses.
        seen: set[int] = set()
        for mod in list(sys.modules.values()):
            if mod is None:
                continue
            cur = getattr(mod, "settings", None)
            if isinstance(cur, _SettingsCls) and id(cur) not in seen:
                seen.add(id(cur))
                cur.api_token = api_token
                cur.database_url = db_url
                cur.jwt_secret = jwt_secret

        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(db_engine):
    """A single depot+instrument with a position; ticker not yet set."""
    with Session(db_engine) as s:
        s.add(Depot(depot_id="DEPOT_X", currency="EUR"))
        s.add(
            Instrument(
                isin=FAKE_ISIN,
                wkn="000000",
                name="Doe AG",
                instrument_type="SHARE",
                currency="EUR",
            )
        )
        s.commit()
        s.add(
            Position(
                depot_id="DEPOT_X",
                isin=FAKE_ISIN,
                quantity=Decimal("10"),
                current_price=Decimal("100"),
                current_value=Decimal("1000"),
                purchase_value=Decimal("800"),
                currency="EUR",
            )
        )
        s.commit()


def _set_ticker(db_engine, ticker: str):
    with Session(db_engine) as s:
        instr = s.get(Instrument, FAKE_ISIN)
        instr.ticker_symbol = ticker
        s.commit()


# ---------------------------------------------------------------------------
# User JWT helper — write endpoints require user-only auth.
# ---------------------------------------------------------------------------


@pytest.fixture
def user_auth(db_engine, api_client):
    """Seed a user row, mint a real JWT, return Authorization headers.

    Depends on ``api_client`` so the JWT_SECRET that the api fixture
    bakes into its Settings singleton is the one we sign with — this
    keeps issuer and verifier in lock-step regardless of test order
    (other tests in the suite reload ``src.core.config`` mid-run).
    """
    import sys
    import uuid as _uuid

    from src.core.db.models import User

    secret = "test-jwt-secret-min-32chars-long-aaaaaaaa"
    user_id = _uuid.uuid4().hex
    with Session(db_engine) as s:
        s.add(
            User(
                id=user_id,
                email="tester@example.com",
                password_hash="dummy-not-used",
                display_name="Tester",
                is_active=True,
            )
        )
        s.commit()

    # Make sure every loaded module's `settings` instance carries the
    # same JWT secret, defending against `importlib.reload` polluters.
    from src.core.config import Settings as _SettingsCls

    seen_ids: set[int] = set()
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        cur = getattr(mod, "settings", None)
        if isinstance(cur, _SettingsCls) and id(cur) not in seen_ids:
            seen_ids.add(id(cur))
            cur.jwt_secret = secret

    from src.api.auth.jwt import issue_token

    token = issue_token(user_id)
    yield {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# PATCH /portfolio/instruments/{isin} — user-only auth
# ---------------------------------------------------------------------------


def test_patch_instrument_sets_ticker(api_client, seeded, db_engine, user_auth):
    r = api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=user_auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ticker_symbol"] == "SAP.DE"

    with Session(db_engine) as s:
        instr = s.get(Instrument, FAKE_ISIN)
        assert instr is not None
        assert instr.ticker_symbol == "SAP.DE"


def test_patch_instrument_blank_clears_ticker(api_client, seeded, user_auth):
    api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=user_auth,
    )
    r = api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "   "},
        headers=user_auth,
    )
    assert r.status_code == 200
    assert r.json()["ticker_symbol"] is None


def test_patch_instrument_unknown_isin_404(api_client, user_auth):
    r = api_client.patch(
        "/api/v1/portfolio/instruments/DE9999999999",
        json={"ticker_symbol": "X"},
        headers=user_auth,
    )
    assert r.status_code == 404


def test_patch_instrument_rejects_service_token(api_client, seeded):
    """Service-token (static API_TOKEN) callers must be locked out of
    the write endpoints — they accept user JWTs only.
    """
    r = api_client.patch(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
        json={"ticker_symbol": "SAP.DE"},
        headers=AUTH,
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /portfolio/instruments/{isin}/backfill-prices — user-only dispatcher
# ---------------------------------------------------------------------------


def test_backfill_dispatcher_forwards_to_worker(
    api_client, seeded, httpx_post_spy, user_auth
):
    httpx_post_spy.return_value = FakeHttpxResponse(
        200,
        {
            "isin": FAKE_ISIN,
            "ticker_symbol": "SAP.DE",
            "requested_from": "2026-04-01",
            "requested_to": "2026-04-03",
            "fetched_points": 3,
            "inserted_points": 3,
            "skipped_existing": 0,
            "source": "yfinance",
        },
    )

    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-03"},
        headers=user_auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched_points"] == 3
    assert body["inserted_points"] == 3

    # Exactly one outbound to the worker, with the canonical payload.
    assert len(httpx_post_spy.calls) == 1
    call = httpx_post_spy.calls[0]
    assert call["url"].endswith("/internal/portfolio/backfill-prices")
    assert call["json"] == {
        "isin": FAKE_ISIN,
        "from_date": "2026-04-01",
        "to_date": "2026-04-03",
    }


def test_backfill_dispatcher_rejects_inverted_range_before_dispatch(
    api_client, seeded, httpx_post_spy, user_auth
):
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-10", "to_date": "2026-04-01"},
        headers=user_auth,
    )
    assert r.status_code == 400
    # No worker round-trip if the api can already say no.
    assert httpx_post_spy.calls == []


def test_backfill_dispatcher_404_for_unknown_isin(
    api_client, httpx_post_spy, user_auth
):
    r = api_client.post(
        "/api/v1/portfolio/instruments/DE9999999999/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-03"},
        headers=user_auth,
    )
    assert r.status_code == 404
    assert httpx_post_spy.calls == []


def test_backfill_dispatcher_propagates_worker_400(
    api_client, seeded, httpx_post_spy, user_auth
):
    httpx_post_spy.return_value = FakeHttpxResponse(
        400, {"detail": "Instrument has no ticker_symbol"}
    )
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=user_auth,
    )
    assert r.status_code == 400
    assert "ticker_symbol" in r.json()["detail"]


def test_backfill_dispatcher_propagates_worker_502(
    api_client, seeded, httpx_post_spy, user_auth
):
    httpx_post_spy.return_value = FakeHttpxResponse(
        502, {"detail": "Upstream price provider failed: rate limited"}
    )
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=user_auth,
    )
    assert r.status_code == 502
    assert "rate limited" in r.json()["detail"]


def test_backfill_dispatcher_unreachable_worker_502(
    api_client, seeded, httpx_post_spy, user_auth
):
    httpx_post_spy.side_effect = httpx.ConnectError("connection refused")
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=user_auth,
    )
    assert r.status_code == 502
    assert "Worker unreachable" in r.json()["detail"]


def test_backfill_dispatcher_rejects_service_token(api_client, seeded, httpx_post_spy):
    """A service-token caller cannot trigger external traffic via the
    dispatcher; the request must 403 before any worker round-trip.
    """
    r = api_client.post(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
        json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        headers=AUTH,
    )
    assert r.status_code == 403
    assert httpx_post_spy.calls == []


# ---------------------------------------------------------------------------
# Worker-side end-to-end (DB write path + yfinance error handling)
# ---------------------------------------------------------------------------


def test_worker_backfill_inserts_points(
    worker_client, seeded, fake_provider, db_engine
):
    _set_ticker(db_engine, "SAP.DE")
    fake_provider.points = [
        PricePoint(date(2026, 4, 1), Decimal("100.50"), "EUR"),
        PricePoint(date(2026, 4, 2), Decimal("101.25"), "EUR"),
        PricePoint(date(2026, 4, 3), Decimal("102.00"), "EUR"),
    ]

    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-01",
            "to_date": "2026-04-03",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched_points"] == 3
    assert body["inserted_points"] == 3
    assert body["skipped_existing"] == 0
    assert body["source"] == "yfinance"
    assert fake_provider.calls == [("SAP.DE", date(2026, 4, 1), date(2026, 4, 3))]

    with Session(db_engine) as s:
        rows = s.query(InstrumentPriceHistory).all()
        assert len(rows) == 3
        assert {r.price_date for r in rows} == {
            date(2026, 4, 1),
            date(2026, 4, 2),
            date(2026, 4, 3),
        }


def test_worker_backfill_idempotent_on_overlap(
    worker_client, seeded, fake_provider, db_engine
):
    _set_ticker(db_engine, "SAP.DE")
    fake_provider.points = [
        PricePoint(date(2026, 4, 1), Decimal("100.0"), "EUR"),
        PricePoint(date(2026, 4, 2), Decimal("101.0"), "EUR"),
    ]
    worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-01",
            "to_date": "2026-04-02",
        },
    )

    fake_provider.points = [
        PricePoint(date(2026, 4, 1), Decimal("100.0"), "EUR"),
        PricePoint(date(2026, 4, 2), Decimal("101.0"), "EUR"),
        PricePoint(date(2026, 4, 3), Decimal("102.0"), "EUR"),
    ]
    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-01",
            "to_date": "2026-04-03",
        },
    )
    body = r.json()
    assert body["fetched_points"] == 3
    assert body["inserted_points"] == 1
    assert body["skipped_existing"] == 2

    with Session(db_engine) as s:
        assert s.query(InstrumentPriceHistory).count() == 3


def test_worker_backfill_requires_ticker(worker_client, seeded):
    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-01",
            "to_date": "2026-04-05",
        },
    )
    assert r.status_code == 400
    assert "ticker_symbol" in r.json()["detail"]


def test_worker_backfill_provider_error_returns_502(
    worker_client, seeded, fake_provider, db_engine
):
    _set_ticker(db_engine, "BOGUS.DE")
    fake_provider.error = PriceFetchError("rate limited")

    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-01",
            "to_date": "2026-04-05",
        },
    )
    assert r.status_code == 502
    # Static detail — upstream exception text is logged but never echoed to
    # the client (security M2: avoid leaking provider internals via 502).
    assert r.json()["detail"] == "Upstream price provider failed"


def test_worker_backfill_empty_response_succeeds(
    worker_client, seeded, fake_provider, db_engine
):
    _set_ticker(db_engine, "X.DE")
    fake_provider.points = []

    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-01",
            "to_date": "2026-04-05",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fetched_points"] == 0
    assert body["inserted_points"] == 0


def test_worker_backfill_rejects_inverted_range(worker_client, seeded, db_engine):
    _set_ticker(db_engine, "SAP.DE")
    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": FAKE_ISIN,
            "from_date": "2026-04-10",
            "to_date": "2026-04-01",
        },
    )
    assert r.status_code == 400


def test_worker_backfill_unknown_isin_404(worker_client):
    r = worker_client.post(
        "/internal/portfolio/backfill-prices",
        json={
            "isin": "DE9999999999",
            "from_date": "2026-04-01",
            "to_date": "2026-04-05",
        },
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /portfolio/instruments/{isin}/prices — read-only, still service-token OK
# ---------------------------------------------------------------------------


def test_get_prices_empty(api_client, seeded):
    r = api_client.get(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/prices",
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json() == []


def test_get_prices_returns_sorted_filtered_series(api_client, seeded, db_engine):
    with Session(db_engine) as s:
        for d, close in [
            (date(2026, 3, 28), Decimal("99.0")),
            (date(2026, 4, 1), Decimal("100.0")),
            (date(2026, 4, 2), Decimal("101.0")),
            (date(2026, 4, 5), Decimal("102.0")),
        ]:
            s.add(
                InstrumentPriceHistory(
                    isin=FAKE_ISIN,
                    price_date=d,
                    close=close,
                    currency="EUR",
                    source="yfinance",
                )
            )
        s.commit()

    r = api_client.get(
        f"/api/v1/portfolio/instruments/{FAKE_ISIN}/prices",
        params={"from": "2026-04-01", "to": "2026-04-04"},
        headers=AUTH,
    )
    assert r.status_code == 200
    series = r.json()
    assert [p["price_date"] for p in series] == ["2026-04-01", "2026-04-02"]
    assert Decimal(series[0]["close"]) == Decimal("100.0")
    assert series[0]["source"] == "yfinance"


def test_get_prices_unknown_isin_404(api_client, seeded):
    r = api_client.get(
        "/api/v1/portfolio/instruments/DE9999999999/prices",
        headers=AUTH,
    )
    assert r.status_code == 404


def test_endpoints_require_auth(api_client, seeded):
    assert (
        api_client.patch(
            f"/api/v1/portfolio/instruments/{FAKE_ISIN}",
            json={"ticker_symbol": "X"},
        ).status_code
        == 401
    )
    assert (
        api_client.post(
            f"/api/v1/portfolio/instruments/{FAKE_ISIN}/backfill-prices",
            json={"from_date": "2026-04-01", "to_date": "2026-04-05"},
        ).status_code
        == 401
    )
    assert (
        api_client.get(
            f"/api/v1/portfolio/instruments/{FAKE_ISIN}/prices"
        ).status_code
        == 401
    )
