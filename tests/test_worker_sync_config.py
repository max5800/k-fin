from unittest.mock import AsyncMock, mock_open, patch

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

    with (
        patch.object(worker_mod.settings, "account_transaction_limit", 500),
        patch.object(worker_mod.settings, "account_transaction_min_booking_date", None),
        patch.object(worker_mod.settings, "depot_transaction_limit", 100),
        patch.object(worker_mod.settings, "depot_transaction_min_booking_date", None),
        patch("scripts.export_csv.export_account_to_csv"),
        patch("scripts.export_csv.export_depot_positions_csv"),
        patch("scripts.export_csv.export_depot_transactions_csv"),
        patch("scripts.export_csv.export_summary_csv"),
        patch("src.exporter.json_export.build_export", return_value={"ok": True}),
        patch("pathlib.Path.mkdir"),
        patch("builtins.open", mock_open()),
        patch("json.dump"),
    ):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/confirm?session_id=sess-123")

    assert resp.status_code == 200
    mock_client.get_all_data.assert_awaited_once_with(
        account_transaction_limit=1200,
        account_transaction_min_booking_date=None,
        depot_transaction_limit=100,
        depot_transaction_min_booking_date="-365d",
    )
