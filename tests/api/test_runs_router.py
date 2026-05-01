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
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import AgentRun, RunStatus, RunTrigger


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


# ---------------------------------------------------------------------------
# DELETE /runs/{run_id}
# ---------------------------------------------------------------------------


class TestDeleteRun:
    def test_cancel_running_returns_204_and_marks_cancelled(
        self, api_client, db_engine
    ):
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
