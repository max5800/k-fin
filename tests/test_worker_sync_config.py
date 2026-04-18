from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from fastapi.testclient import TestClient

import main as worker_mod


def test_internal_sync_start_stores_request_overrides():
    worker_mod._pending_sessions.clear()

    mock_client = AsyncMock()
    mock_client.begin_auth.return_value = {
        "session_identifier": "sess-123",
        "challenge_id": "challenge-1",
    }

    with patch("main.ComdirectClient", return_value=mock_client):
        client = TestClient(worker_mod.app)
        resp = client.post(
            "/internal/sync/start",
            json={
                "account_transaction_limit": 1200,
                "depot_transaction_min_booking_date": "-365d",
            },
        )

    assert resp.status_code == 200
    session_id = resp.json()["session_id"]
    assert worker_mod._pending_sessions[session_id]["config"] == {
        "account_transaction_limit": 1200,
        "depot_transaction_min_booking_date": "-365d",
    }


def _make_confirm_patches(*, ingest_mock=None, pipeline_mock=None):
    """Build the common context-manager stack for confirm tests.

    Normalization patches come first (before builtins.open) so module
    imports don't break when pandas tries to use real open().
    """
    patches = [
        patch.object(worker_mod.settings, "account_transaction_limit", 500),
        patch.object(worker_mod.settings, "account_transaction_min_booking_date", None),
        patch.object(worker_mod.settings, "depot_transaction_limit", 100),
        patch.object(worker_mod.settings, "depot_transaction_min_booking_date", None),
        # Normalization mocks — MUST come before builtins.open mock
        patch("src.normalization.ingest.ingest_json_export", ingest_mock or MagicMock(return_value=0)),
        patch("src.normalization.pipeline.NormalizationPipeline", pipeline_mock or MagicMock()),
        # CSV/JSON export mocks
        patch("scripts.export_csv.export_account_to_csv"),
        patch("scripts.export_csv.export_depot_positions_csv"),
        patch("scripts.export_csv.export_depot_transactions_csv"),
        patch("scripts.export_csv.export_summary_csv"),
        patch("src.exporter.json_export.build_export", return_value={"ok": True}),
        patch("pathlib.Path.mkdir"),
        patch("builtins.open", mock_open()),
        patch("json.dump"),
    ]
    return patches


def _enter_all(patches):
    """Enter a list of context managers, return a cleanup function."""
    entered = []
    for p in patches:
        entered.append(p.__enter__())
    return entered


def _exit_all(patches):
    for p in reversed(patches):
        p.__exit__(None, None, None)


def test_internal_sync_confirm_uses_session_overrides_with_fallbacks(tmp_path):
    worker_mod._pending_sessions.clear()

    mock_client = AsyncMock()
    mock_client.complete_auth.return_value = True
    mock_client.get_all_data.return_value = {
        "accounts": [],
        "transactions": {},
        "depots": [],
        "depot_positions": {},
        "depot_transactions": {},
    }

    worker_mod._pending_sessions["sess-123"] = {
        "client": mock_client,
        "session_identifier": "sid",
        "challenge_id": "cid",
        "config": {
            "account_transaction_limit": 1200,
            "depot_transaction_min_booking_date": "-365d",
        },
    }

    patches = _make_confirm_patches()
    _enter_all(patches)
    try:
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/confirm?session_id=sess-123")
    finally:
        _exit_all(patches)

    assert resp.status_code == 200
    mock_client.get_all_data.assert_awaited_once_with(
        account_transaction_limit=1200,
        account_transaction_min_booking_date=None,
        depot_transaction_limit=100,
        depot_transaction_min_booking_date="-365d",
    )


def _make_confirm_session(session_id="sess-ingest"):
    """Helper: set up a pending session with a mock client."""
    worker_mod._pending_sessions.clear()
    mock_client = AsyncMock()
    mock_client.complete_auth.return_value = True
    mock_client.get_all_data.return_value = {
        "accounts": [],
        "transactions": {},
        "depots": [],
        "depot_positions": {},
        "depot_transactions": {},
    }
    worker_mod._pending_sessions[session_id] = {
        "client": mock_client,
        "session_identifier": "sid",
        "challenge_id": "cid",
        "config": {},
    }


def test_internal_sync_confirm_calls_ingest_and_normalize():
    """After JSON export, worker must ingest into DB and normalize."""
    _make_confirm_session()

    mock_ingest = MagicMock(return_value=5)
    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.process_and_normalize.return_value = MagicMock(__len__=lambda self: 5)
    mock_pipeline_cls = MagicMock(return_value=mock_pipeline_instance)

    patches = _make_confirm_patches(
        ingest_mock=mock_ingest, pipeline_mock=mock_pipeline_cls
    )
    _enter_all(patches)
    try:
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/confirm?session_id=sess-ingest")
    finally:
        _exit_all(patches)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["ingest"]["inserted"] == 5
    assert data["ingest"]["normalized"] == 5
    # depot ingest runs best-effort; with the mocked engine backing an empty
    # DB the depot counters may be None or all-zero, but the key must exist.
    assert "depots" in data["ingest"]
    mock_ingest.assert_called_once()
    mock_pipeline_instance.process_and_normalize.assert_called_once()


def test_internal_sync_confirm_export_succeeds_when_ingest_fails():
    """Ingest failure must not break the export response."""
    _make_confirm_session()

    mock_ingest = MagicMock(side_effect=RuntimeError("DB is down"))

    patches = _make_confirm_patches(ingest_mock=mock_ingest)
    _enter_all(patches)
    try:
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/confirm?session_id=sess-ingest")
    finally:
        _exit_all(patches)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["ingest"] is None
