"""Worker endpoint tests for the historical backfill flow.

The actual walker is exercised in test_backfill.py — here we cover the
HTTP surface only: TAN session handoff, concurrency guard, status
endpoint.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main as worker_mod
from src.core.db.models import Base, BackfillRun, BackfillStatus


def _make_engine_with_table(monkeypatch):
    """Stand up an in-memory sqlite engine that has the backfill_runs table.

    Uses StaticPool + check_same_thread=False so the same in-memory DB
    is visible across threads (TestClient runs handlers in a thread).
    Installs the engine on ``app.state`` via :func:`worker_mod.set_test_engine`
    so the new ``get_engine`` dependency resolves to this one — the
    previous "monkeypatch create_engine" trick stopped working once the
    handlers switched to reusing ``app.state.engine``.

    Also stubs out ``engine.dispose`` because the lifespan disposes on
    shutdown and a couple of tests reuse the same engine across multiple
    TestClient instances.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    engine.dispose = lambda: None  # type: ignore[method-assign]

    # Pin app.state.engine for the new get_engine dep, AND keep the
    # legacy create_engine override so any helper pipeline that still
    # builds its own engine via settings.database_url ends up pointing
    # at the same in-memory DB.
    worker_mod.set_test_engine(engine)
    monkeypatch.setattr(worker_mod, "create_engine", lambda *_a, **_kw: engine)
    return engine


# ----------------------------------------------------------------------
# /internal/backfill/start
# ----------------------------------------------------------------------


def test_backfill_start_triggers_tan_and_stores_session(monkeypatch):
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)

    mock_client = AsyncMock()
    mock_client.begin_auth.return_value = {
        "session_identifier": "sess-id",
        "challenge_id": "challenge-id",
    }
    with patch("main.ComdirectClient", return_value=mock_client):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/backfill/start", json={"months": 12})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_tan"
    sid = body["session_id"]
    assert worker_mod._pending_sessions[sid]["kind"] == "backfill"
    assert worker_mod._pending_sessions[sid]["config"] == {"months": 12}


def test_backfill_start_uses_default_months_when_no_body(monkeypatch):
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)

    mock_client = AsyncMock()
    mock_client.begin_auth.return_value = {
        "session_identifier": "sess-id",
        "challenge_id": "challenge-id",
    }
    with patch("main.ComdirectClient", return_value=mock_client):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/backfill/start")

    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert worker_mod._pending_sessions[sid]["config"] == {"months": 24}


def test_backfill_start_rejects_months_outside_range(monkeypatch):
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)

    mock_client = AsyncMock()
    with patch("main.ComdirectClient", return_value=mock_client):
        client = TestClient(worker_mod.app)
        # Below range
        resp = client.post("/internal/backfill/start", json={"months": 0})
        assert resp.status_code == 422
        # Above range (cap is 24)
        resp = client.post("/internal/backfill/start", json={"months": 36})
        assert resp.status_code == 422


def test_backfill_start_409_when_run_already_in_flight(monkeypatch):
    worker_mod._pending_sessions.clear()
    engine = _make_engine_with_table(monkeypatch)

    # Seed a running BackfillRun row
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(
            BackfillRun(
                id="already-running",
                target_start_date=date(2024, 1, 1),
                status=BackfillStatus.RUNNING,
            )
        )
        s.commit()

    mock_client = AsyncMock()
    with patch("main.ComdirectClient", return_value=mock_client):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/backfill/start")

    assert resp.status_code == 409
    assert "läuft bereits" in resp.json()["detail"]
    # begin_auth was NOT called — no needless TAN push
    mock_client.begin_auth.assert_not_called()


def test_backfill_start_502_on_begin_auth_failure(monkeypatch):
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)

    mock_client = AsyncMock()
    mock_client.begin_auth.side_effect = RuntimeError("step 2 failed")
    with patch("main.ComdirectClient", return_value=mock_client):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/backfill/start")

    assert resp.status_code == 502


# ----------------------------------------------------------------------
# /internal/backfill/confirm
# ----------------------------------------------------------------------


def test_backfill_confirm_completes_auth_creates_run_dispatches_task(monkeypatch):
    worker_mod._pending_sessions.clear()
    engine = _make_engine_with_table(monkeypatch)

    mock_client = AsyncMock()
    mock_client.complete_auth.return_value = True

    worker_mod._pending_sessions["sess-x"] = {
        "kind": "backfill",
        "client": mock_client,
        "session_identifier": "sid",
        "challenge_id": "cid",
        "config": {"months": 6},
    }

    # Stop the actual background task from running so we don't hit the network
    captured: dict = {}

    def fake_add_task(self, fn, *args, **kwargs):  # noqa: ARG001 — bound-method shape
        captured["fn"] = fn
        captured["args"] = args

    with patch.object(worker_mod.BackgroundTasks, "add_task", fake_add_task):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/backfill/confirm?session_id=sess-x")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert "run_id" in body

    # BackgroundTask was scheduled, with run_id + client + target ISO
    assert captured["fn"] is worker_mod._execute_backfill_in_worker
    run_id, client_arg, target_iso = captured["args"]
    assert run_id == body["run_id"]
    assert client_arg is mock_client
    # 6 months back from today's first-of-month
    today_first = date.today().replace(day=1)
    expected_year = today_first.year
    expected_month = today_first.month - 6
    while expected_month <= 0:
        expected_month += 12
        expected_year -= 1
    assert target_iso == today_first.replace(
        year=expected_year, month=expected_month
    ).isoformat()

    # BackfillRun row was created
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        row = s.get(BackfillRun, run_id)
        assert row is not None
        assert row.status == BackfillStatus.RUNNING


def test_backfill_confirm_404_for_unknown_session(monkeypatch):
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)
    client = TestClient(worker_mod.app)
    resp = client.post("/internal/backfill/confirm?session_id=does-not-exist")
    assert resp.status_code == 404


def test_backfill_confirm_400_when_session_kind_is_sync(monkeypatch):
    """Confirm endpoint rejects sessions that weren't started for backfill."""
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)

    worker_mod._pending_sessions["sync-sess"] = {
        # No 'kind' key (the sync flow doesn't set one) → treated as non-backfill
        "client": AsyncMock(),
        "session_identifier": "sid",
        "challenge_id": "cid",
        "config": {},
    }
    client = TestClient(worker_mod.app)
    resp = client.post("/internal/backfill/confirm?session_id=sync-sess")
    assert resp.status_code == 400


def test_backfill_confirm_502_when_complete_auth_fails(monkeypatch):
    worker_mod._pending_sessions.clear()
    _make_engine_with_table(monkeypatch)

    mock_client = AsyncMock()
    mock_client.complete_auth.return_value = False
    worker_mod._pending_sessions["sess-y"] = {
        "kind": "backfill",
        "client": mock_client,
        "session_identifier": "sid",
        "challenge_id": "cid",
        "config": {"months": 24},
    }
    client = TestClient(worker_mod.app)
    resp = client.post("/internal/backfill/confirm?session_id=sess-y")
    assert resp.status_code == 502


# ----------------------------------------------------------------------
# /internal/backfill/runs/{run_id}
# ----------------------------------------------------------------------


def test_backfill_run_status_returns_progress(monkeypatch):
    engine = _make_engine_with_table(monkeypatch)

    from sqlalchemy.orm import Session

    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    with Session(engine) as s:
        s.add(
            BackfillRun(
                id="run-1",
                target_start_date=date(2024, 1, 1),
                current_window_start=date(2024, 6, 1),
                windows_total=24,
                windows_done=10,
                rows_inserted=153,
                progress_message="ACC-1: 2024-06-01..2024-06-30 (+30)",
                status=BackfillStatus.RUNNING,
                started_at=started,
            )
        )
        s.commit()

    client = TestClient(worker_mod.app)
    resp = client.get("/internal/backfill/runs/run-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["status"] == "running"
    assert body["windows_total"] == 24
    assert body["windows_done"] == 10
    assert body["rows_inserted"] == 153
    assert body["target_start_date"] == "2024-01-01"
    assert body["current_window_start"] == "2024-06-01"
    assert body["progress_message"] == "ACC-1: 2024-06-01..2024-06-30 (+30)"
    assert body["finished_at"] is None
    assert body["error"] is None


def test_backfill_run_status_404(monkeypatch):
    _make_engine_with_table(monkeypatch)
    client = TestClient(worker_mod.app)
    resp = client.get("/internal/backfill/runs/does-not-exist")
    assert resp.status_code == 404


def test_backfill_run_status_terminal_failed(monkeypatch):
    engine = _make_engine_with_table(monkeypatch)

    from sqlalchemy.orm import Session

    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    finished = datetime.now(timezone.utc)
    with Session(engine) as s:
        s.add(
            BackfillRun(
                id="run-fail",
                target_start_date=date(2024, 1, 1),
                status=BackfillStatus.FAILED,
                error="boom",
                started_at=started,
                finished_at=finished,
            )
        )
        s.commit()

    client = TestClient(worker_mod.app)
    resp = client.get("/internal/backfill/runs/run-fail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "boom"
    assert body["finished_at"] is not None


# ----------------------------------------------------------------------
# get_engine dep — refactor regression guard
# ----------------------------------------------------------------------


def test_get_engine_returns_app_state_engine(monkeypatch):
    """Handlers should consume the lifespan-built engine, not build one."""
    from unittest.mock import MagicMock

    fake_engine = MagicMock(name="lifespan-engine")
    worker_mod.set_test_engine(fake_engine)
    try:
        # Build a request stand-in: the dep only inspects request.app.state.
        request = MagicMock()
        request.app = worker_mod.app
        assert worker_mod.get_engine(request) is fake_engine
    finally:
        if hasattr(worker_mod.app.state, "engine"):
            del worker_mod.app.state.engine


def test_get_engine_503_when_engine_missing(monkeypatch):
    """If lifespan hasn't run, surface 503 rather than a confusing 500."""
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    # Make sure no leftover engine is hanging around.
    if hasattr(worker_mod.app.state, "engine"):
        del worker_mod.app.state.engine

    request = MagicMock()
    request.app = worker_mod.app
    try:
        worker_mod.get_engine(request)
        raise AssertionError("expected HTTPException")
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "engine" in exc.detail.lower()


def test_handlers_do_not_call_create_engine_per_request(monkeypatch):
    """Regression guard: the per-request create_engine() pattern is gone.

    The lifespan still uses ``create_engine`` once at boot — that's fine.
    Inside handler bodies, however, no call should hit
    ``main.create_engine`` since they now reuse ``app.state.engine``.
    """
    engine = _make_engine_with_table(monkeypatch)

    call_count = {"n": 0}
    real_create = worker_mod.create_engine  # the lambda installed by helper

    def counting_create_engine(*args, **kwargs):
        call_count["n"] += 1
        return real_create(*args, **kwargs)

    monkeypatch.setattr(worker_mod, "create_engine", counting_create_engine)

    # Hit the status endpoint: this used to build + dispose an engine
    # on every call.
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(
            BackfillRun(
                id="reg-1",
                target_start_date=date(2024, 1, 1),
                status=BackfillStatus.RUNNING,
            )
        )
        s.commit()

    client = TestClient(worker_mod.app)
    for _ in range(3):
        resp = client.get("/internal/backfill/runs/reg-1")
        assert resp.status_code == 200

    assert call_count["n"] == 0, (
        f"create_engine called {call_count['n']} time(s) — handlers should "
        "use the shared app.state.engine via get_engine"
    )
