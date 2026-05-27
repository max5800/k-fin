"""Tests for the runs router (/api/v1/runs).

The api creates the AgentRun row, then POSTs to the worker. We mock the
dispatch — there's no live worker in CI. DELETE flips the row to
CANCELLED; the worker's _update_progress observes the flip on its next
heartbeat.

The runs-router import is lazy: importing it at module load captures
`src.core.config.settings`, but `tests/auth/test_jwt.py` reloads the
config module mid-suite and would leave a stale singleton bound here.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import (
    AgentRun,
    AppSettings,
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    RunStatus,
    RunTrigger,
)


@pytest.fixture
def runs_router():
    """Lazy import so the module's `settings` capture is fresh."""
    from src.api.routers import runs as _runs

    return _runs


AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def api_client(db_engine):
    """A TestClient with DB overridden to the test engine and the worker
    dispatch stubbed to a no-op (per-test override below if needed)."""
    from src.core.db import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    db_url = db_engine.url.render_as_string(hide_password=False)
    with patch.dict(
        os.environ,
        {
            "API_TOKEN": "test-secret",
            "DATABASE_URL": db_url,
            "WORKER_URL": "http://worker.test",
        },
    ):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


def _seed_run(
    db_engine,
    *,
    run_id: str,
    status: RunStatus,
    finished_at: datetime | None = None,
) -> None:
    with Session(db_engine) as s:
        s.add(
            AgentRun(
                id=run_id,
                agent_name="categorization",
                status=status,
                trigger=RunTrigger.MANUAL,
                started_at=datetime.now(timezone.utc),
                finished_at=finished_at,
            )
        )
        s.commit()


def _seed_pending_tx(
    session: Session,
    *,
    tx_id: str,
    raw_hash: str,
    source: DataSource,
    internal_transfer: bool = False,
) -> None:
    session.add(
        RawTransaction(
            content_hash=raw_hash,
            source=source,
            external_id=tx_id,
            raw_data={"stub": True},
        )
    )
    session.flush()
    session.add(
        NormalizedTransaction(
            id=tx_id,
            raw_content_hash=raw_hash,
            source=source,
            external_id=tx_id,
            booking_date=date(2026, 5, 20),
            valuation_date=date(2026, 5, 20),
            amount=Decimal("-12.34"),
            currency="EUR",
            sender="John Doe",
            recipient="Demo Merchant",
            description="test fixture",
            category_id=None,
            is_recurring=False,
            is_outlier=False,
            internal_transfer=internal_transfer,
        )
    )


# ---------------------------------------------------------------------------
# POST /runs/{agent_name} — dispatch happy path
# ---------------------------------------------------------------------------


class TestStartRunDispatch:
    def test_dispatches_to_worker_and_returns_pending(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        calls: list[tuple[str, dict]] = []

        def fake_dispatch(path: str, payload: dict) -> None:
            calls.append((path, payload))

        monkeypatch.setattr(runs_router, "_dispatch_to_worker", fake_dispatch)

        resp = api_client.post("/api/v1/runs/categorization", headers=AUTH)
        assert resp.status_code == 201
        body = resp.json()
        assert body["agent_name"] == "categorization"
        assert body["status"] == "pending"
        run_id = body["id"]

        assert len(calls) == 1
        path, payload = calls[0]
        assert path == "/internal/runs/start?agent_name=categorization"
        assert payload == {"run_id": run_id}

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.PENDING

    def test_dispatch_failure_marks_row_failed(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        def boom(path: str, payload: dict) -> None:
            raise httpx.HTTPError("worker offline")

        # Invoke the real _dispatch_to_worker so we exercise its exception
        # mapping, but stub httpx.post under it to raise.
        def fake_post(*_a, **_kw):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(runs_router.httpx, "post", fake_post)

        resp = api_client.post("/api/v1/runs/categorization", headers=AUTH)
        assert resp.status_code == 502

        # Row should exist and be FAILED.
        with Session(db_engine) as s:
            rows = s.query(AgentRun).all()
            assert len(rows) == 1
            assert rows[0].status == RunStatus.FAILED
            assert rows[0].error == "Worker dispatch failed"
            assert rows[0].finished_at is not None
        # Reference `boom` to satisfy lint without altering wiring.
        _ = boom

    def test_full_run_dispatches_to_start_full(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post("/api/v1/runs/full", headers=AUTH)
        assert resp.status_code == 201
        run_id = resp.json()["id"]

        assert calls == [("/internal/runs/start-full", {"run_id": run_id})]

    def test_dispatch_default_omits_period_days(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        """Without ?period_days the worker payload stays minimal so agents
        keep their built-in defaults (anomaly: 30d, weekly: ISO week, …)."""
        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post("/api/v1/runs/anomaly", headers=AUTH)
        assert resp.status_code == 201

        assert len(calls) == 1
        _, payload = calls[0]
        assert "period_days" not in payload

    def test_dispatch_forwards_period_days_for_single_agent(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post("/api/v1/runs/anomaly?period_days=14", headers=AUTH)
        assert resp.status_code == 201
        run_id = resp.json()["id"]

        assert calls == [
            (
                "/internal/runs/start?agent_name=anomaly",
                {"run_id": run_id, "period_days": 14},
            )
        ]

    def test_dispatch_forwards_period_days_for_full_run(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post("/api/v1/runs/full?period_days=90", headers=AUTH)
        assert resp.status_code == 201
        run_id = resp.json()["id"]

        assert calls == [
            (
                "/internal/runs/start-full",
                {"run_id": run_id, "period_days": 90},
            )
        ]

    def test_period_days_out_of_range_returns_422(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        # Don't even bother dispatching for an invalid period.
        monkeypatch.setattr(runs_router, "_dispatch_to_worker", lambda *_a, **_kw: None)

        resp = api_client.post("/api/v1/runs/weekly_analysis?period_days=0", headers=AUTH)
        assert resp.status_code == 422

        resp = api_client.post("/api/v1/runs/weekly_analysis?period_days=99999", headers=AUTH)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /runs/health
# ---------------------------------------------------------------------------


class TestRunsHealth:
    def test_returns_categorization_health_metrics(self, api_client, db_engine):
        now = datetime.now(timezone.utc)
        with Session(db_engine) as s:
            s.add(AppSettings(id=1, auto_apply_confidence=Decimal("0.60")))
            s.add_all(
                [
                    AgentRun(
                        id="health-categorization",
                        agent_name="categorization",
                        status=RunStatus.SUCCEEDED,
                        trigger=RunTrigger.MANUAL,
                        started_at=now - timedelta(days=1),
                        finished_at=now,
                        result={
                            "categorization": {
                                "suggestions": [
                                    {
                                        "transaction_id": "tx-a",
                                        "suggested_category_id": "groceries",
                                        "confidence": 0.9,
                                        "reasoning": "fixture",
                                    },
                                    {
                                        "transaction_id": "tx-b",
                                        "suggested_category_id": "fun",
                                        "confidence": 0.4,
                                        "reasoning": "fixture",
                                    },
                                ],
                                "high_confidence_count": 1,
                            }
                        },
                        usage_detail={
                            "categorization": {
                                "memory": {
                                    "hits_per_batch": [2, 0, 3],
                                    "batches_total": 3,
                                    "batches_with_memory": 2,
                                    "low_conf_with_memory": 1,
                                    "low_conf_without_memory": 1,
                                }
                            }
                        },
                    ),
                    AgentRun(
                        id="health-full",
                        agent_name="full",
                        status=RunStatus.SUCCEEDED,
                        trigger=RunTrigger.MANUAL,
                        started_at=now - timedelta(days=2),
                        finished_at=now,
                        result={
                            "categorization": {
                                "suggestions": [
                                    {
                                        "transaction_id": "tx-c",
                                        "suggested_category_id": "rent",
                                        "confidence": 0.8,
                                        "reasoning": "fixture",
                                    },
                                    {
                                        "transaction_id": "tx-d",
                                        "suggested_category_id": "fun",
                                        "confidence": 0.7,
                                        "reasoning": "fixture",
                                    },
                                ]
                            }
                        },
                    ),
                    AgentRun(
                        id="health-old",
                        agent_name="categorization",
                        status=RunStatus.SUCCEEDED,
                        trigger=RunTrigger.MANUAL,
                        started_at=now - timedelta(days=30),
                        finished_at=now,
                        result={
                            "categorization": {
                                "suggestions": [
                                    {
                                        "transaction_id": "tx-old",
                                        "suggested_category_id": "rent",
                                        "confidence": 1.0,
                                        "reasoning": "fixture",
                                    }
                                ],
                                "high_confidence_count": 1,
                            }
                        },
                    ),
                ]
            )
            _seed_pending_tx(
                s,
                tx_id="pending-paypal",
                raw_hash="a" * 64,
                source=DataSource.PAYPAL,
            )
            _seed_pending_tx(
                s,
                tx_id="pending-santander",
                raw_hash="b" * 64,
                source=DataSource.SANTANDER_CC,
            )
            _seed_pending_tx(
                s,
                tx_id="pending-internal",
                raw_hash="c" * 64,
                source=DataSource.COMDIRECT,
                internal_transfer=True,
            )
            s.commit()

        resp = api_client.get("/api/v1/runs/health?window_days=7", headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["window_days"] == 7
        assert body["threshold"] == pytest.approx(0.60)
        assert body["runs_total"] == 2
        assert body["suggestions_total"] == 4
        assert body["high_confidence_total"] == 3
        assert body["auto_apply_rate"] == pytest.approx(0.75)
        assert body["avg_confidence"] == pytest.approx(0.7)
        assert body["memory_batches_total"] == 3
        assert body["memory_batches_with_hits"] == 2
        assert body["memory_hit_rate"] == pytest.approx(2 / 3)
        assert body["memory_hits_total"] == 5
        assert body["low_conf_with_memory"] == 1
        assert body["low_conf_without_memory"] == 1
        assert body["pending_total"] == 2
        assert body["pending_by_source"] == [
            {"source": "paypal", "pending": 1},
            {"source": "santander_cc", "pending": 1},
        ]


# ---------------------------------------------------------------------------
# DELETE /runs/{run_id}
# ---------------------------------------------------------------------------


class TestDeleteRun:
    def test_cancel_running_returns_204_and_marks_cancelled(self, api_client, db_engine):
        run_id = "running-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)

        resp = api_client.delete(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 204

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.CANCELLED
            assert row.error == "cancelled by user"
            assert row.last_error is None
            assert row.finished_at is not None

    def test_cancel_pending_returns_204(self, api_client, db_engine):
        """A pending run can be cancelled before the worker even picks it up."""
        run_id = "pending-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.PENDING)

        resp = api_client.delete(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 204

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.CANCELLED

    def test_cancel_succeeded_returns_409(self, api_client, db_engine):
        run_id = "succeeded-1"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(timezone.utc),
        )

        resp = api_client.delete(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 409
        assert "succeeded" in resp.json()["detail"]

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            # Untouched.
            assert row.status == RunStatus.SUCCEEDED

    def test_cancel_failed_returns_409(self, api_client, db_engine):
        run_id = "failed-1"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
        )

        resp = api_client.delete(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 409

    def test_cancel_already_cancelled_returns_409(self, api_client, db_engine):
        run_id = "cancelled-1"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.CANCELLED,
            finished_at=datetime.now(timezone.utc),
        )

        resp = api_client.delete(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 409

    def test_cancel_unknown_returns_404(self, api_client):
        resp = api_client.delete("/api/v1/runs/does-not-exist", headers=AUTH)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/rerun
# ---------------------------------------------------------------------------


class TestRerunRun:
    def test_rerun_unknown_returns_404(self, api_client):
        resp = api_client.post("/api/v1/runs/does-not-exist/rerun", headers=AUTH)
        assert resp.status_code == 404

    def test_rerun_running_returns_400(self, api_client, db_engine):
        run_id = "running-rerun"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)

        resp = api_client.post(f"/api/v1/runs/{run_id}/rerun", headers=AUTH)
        assert resp.status_code == 400
        assert "running" in resp.json()["detail"]

    def test_rerun_succeeded_returns_400(self, api_client, db_engine):
        run_id = "succeeded-rerun"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(timezone.utc),
        )

        resp = api_client.post(f"/api/v1/runs/{run_id}/rerun", headers=AUTH)
        assert resp.status_code == 400
        assert "succeeded" in resp.json()["detail"]

    def test_rerun_failed_dispatches_new_run(self, api_client, db_engine, monkeypatch, runs_router):
        run_id = "failed-rerun"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
        )

        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post(f"/api/v1/runs/{run_id}/rerun", headers=AUTH)
        assert resp.status_code == 201
        body = resp.json()
        new_id = body["id"]
        assert new_id != run_id
        assert body["agent_name"] == "categorization"
        assert body["status"] == "pending"
        assert body["trigger"] == "manual"

        # Original row is untouched.
        with Session(db_engine) as s:
            original = s.get(AgentRun, run_id)
            assert original.status == RunStatus.FAILED

            new_row = s.get(AgentRun, new_id)
            assert new_row is not None
            assert new_row.agent_name == "categorization"
            assert new_row.status == RunStatus.PENDING

        assert calls == [("/internal/runs/start?agent_name=categorization", {"run_id": new_id})]

    def test_rerun_cancelled_dispatches_new_run(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        run_id = "cancelled-rerun"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.CANCELLED,
            finished_at=datetime.now(timezone.utc),
        )

        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post(f"/api/v1/runs/{run_id}/rerun", headers=AUTH)
        assert resp.status_code == 201
        new_id = resp.json()["id"]

        assert calls == [("/internal/runs/start?agent_name=categorization", {"run_id": new_id})]

    def test_rerun_full_run_uses_start_full_path(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        run_id = "failed-full"
        with Session(db_engine) as s:
            s.add(
                AgentRun(
                    id=run_id,
                    agent_name="full",
                    status=RunStatus.FAILED,
                    trigger=RunTrigger.MANUAL,
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                )
            )
            s.commit()

        calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            runs_router,
            "_dispatch_to_worker",
            lambda p, payload: calls.append((p, payload)),
        )

        resp = api_client.post(f"/api/v1/runs/{run_id}/rerun", headers=AUTH)
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert resp.json()["agent_name"] == "full"

        assert calls == [("/internal/runs/start-full", {"run_id": new_id})]

    def test_rerun_dispatch_failure_marks_new_row_failed(
        self, api_client, db_engine, monkeypatch, runs_router
    ):
        run_id = "failed-then-fail"
        _seed_run(
            db_engine,
            run_id=run_id,
            status=RunStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
        )

        def fake_post(*_a, **_kw):
            raise httpx.ConnectError("worker offline")

        monkeypatch.setattr(runs_router.httpx, "post", fake_post)

        resp = api_client.post(f"/api/v1/runs/{run_id}/rerun", headers=AUTH)
        assert resp.status_code == 502

        with Session(db_engine) as s:
            rows = s.query(AgentRun).order_by(AgentRun.id).all()
            # Original + new failed row.
            assert len(rows) == 2
            new_row = next(r for r in rows if r.id != run_id)
            assert new_row.status == RunStatus.FAILED
            assert new_row.error == "Worker dispatch failed"
            assert new_row.finished_at is not None
