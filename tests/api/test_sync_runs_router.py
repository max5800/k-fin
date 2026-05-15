"""Tests for GET /api/v1/sync/runs — the sync run history endpoint."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.core.db.models import SyncRun, SyncStage, SyncStatus

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def api_client(db_engine):
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


def _seed(
    db_engine,
    *,
    source: SyncStage = SyncStage.RAW_IMPORT,
    status: SyncStatus = SyncStatus.SUCCEEDED,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    rows: int = 0,
    error: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    with Session(db_engine) as s:
        s.add(
            SyncRun(
                id=run_id,
                source=source,
                status=status,
                started_at=started_at or datetime.now(timezone.utc),
                finished_at=finished_at,
                rows_processed=rows,
                error=error,
            )
        )
        s.commit()
    return run_id


def test_returns_empty_list_when_no_runs(api_client):
    resp = api_client.get("/api/v1/sync/runs", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_returns_runs_newest_first(api_client, db_engine):
    base = datetime.now(timezone.utc)
    older = _seed(db_engine, started_at=base - timedelta(hours=2))
    newer = _seed(db_engine, started_at=base - timedelta(minutes=5))

    resp = api_client.get("/api/v1/sync/runs", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body] == [newer, older]


def test_serialises_all_fields_including_error(api_client, db_engine):
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    finished = started + timedelta(seconds=42)
    run_id = _seed(
        db_engine,
        source=SyncStage.NORMALIZE,
        status=SyncStatus.FAILED,
        started_at=started,
        finished_at=finished,
        rows=17,
        error="boom",
    )

    resp = api_client.get("/api/v1/sync/runs", headers=AUTH)
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["id"] == run_id
    assert row["source"] == "normalize"
    assert row["status"] == "failed"
    assert row["rows_processed"] == 17
    assert row["error"] == "boom"
    assert row["started_at"].startswith(started.isoformat()[:19])
    assert row["finished_at"].startswith(finished.isoformat()[:19])


def test_limit_caps_response_size(api_client, db_engine):
    base = datetime.now(timezone.utc)
    for i in range(5):
        _seed(db_engine, started_at=base - timedelta(minutes=i))

    resp = api_client.get("/api/v1/sync/runs?limit=2", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_source_filter(api_client, db_engine):
    _seed(db_engine, source=SyncStage.RAW_IMPORT)
    _seed(db_engine, source=SyncStage.NORMALIZE)

    resp = api_client.get("/api/v1/sync/runs?source=normalize", headers=AUTH)
    body = resp.json()
    assert len(body) == 1
    assert body[0]["source"] == "normalize"


def test_requires_auth(api_client):
    resp = api_client.get("/api/v1/sync/runs")
    assert resp.status_code == 401


def test_limit_validation_rejects_zero_and_negative(api_client):
    assert api_client.get("/api/v1/sync/runs?limit=0", headers=AUTH).status_code == 422
    assert api_client.get("/api/v1/sync/runs?limit=-1", headers=AUTH).status_code == 422
