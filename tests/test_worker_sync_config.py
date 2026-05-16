"""Worker sync-route tests — the provider-neutral
``/internal/sync/{source_id}`` + ``/complete`` endpoints (M16-P2a).

These replace the bank-specific ``/internal/sync/start`` + ``/confirm``
routes. The provider lifecycle (auth, fetch, ingest) is stubbed; the
focus here is route wiring: registry lookup, session storage, the
``provider`` response block, and the fetch-vs-persist error split.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import main as worker_mod
from src.core.db.models import DataSource
from src.external.comdirect_provider import ComdirectProvider


@pytest.fixture(autouse=True)
def _seed_app_state_engine():
    """Pin a no-op engine on app.state so the get_engine dep resolves.

    The TestClient skips lifespan unless used as a context manager, so
    the suite seeds the shared engine itself. A MagicMock is enough —
    the happy-path tests stub the ingest pipeline entirely and the
    engine is only touched on the failure-notify path.
    """
    worker_mod.set_test_engine(MagicMock(name="fake-engine"))
    yield
    if hasattr(worker_mod.app.state, "engine"):
        del worker_mod.app.state.engine


# ---------------------------------------------------------------------------
# POST /internal/sync/{source_id}  — step 1
# ---------------------------------------------------------------------------


def test_internal_sync_start_stores_provider_and_request_overrides():
    worker_mod._pending_sessions.clear()

    mock_client = AsyncMock()
    mock_client.begin_auth.return_value = {
        "session_identifier": "sess-123",
        "challenge_id": "challenge-1",
    }

    with patch(
        "src.external.comdirect_provider.ComdirectClient", return_value=mock_client
    ):
        client = TestClient(worker_mod.app)
        resp = client.post(
            "/internal/sync/comdirect",
            json={
                "account_transaction_limit": 1200,
                "depot_transaction_min_booking_date": "-365d",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_tan"
    # The provider block drives the UI TAN modal.
    assert body["provider"] == {
        "source": "comdirect",
        "display_name": "Comdirect",
        "tan_kind": "decoupled_app_push",
        "display_hint": "photoTAN",
    }

    session = worker_mod._pending_sessions[body["session_id"]]
    assert session["kind"] == "sync"
    assert session["source"] is DataSource.COMDIRECT
    provider = session["provider"]
    assert isinstance(provider, ComdirectProvider)
    # The request body is threaded onto the provider instance.
    assert provider._config == {
        "account_transaction_limit": 1200,
        "depot_transaction_min_booking_date": "-365d",
    }


def test_internal_sync_start_unknown_source_returns_404():
    worker_mod._pending_sessions.clear()
    client = TestClient(worker_mod.app)
    resp = client.post("/internal/sync/not_a_bank")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /internal/sync/{source_id}/complete  — step 2
# ---------------------------------------------------------------------------


def _make_sync_session(session_id: str = "sess-1", config: dict | None = None):
    """Register a pending sync session with a Comdirect provider whose
    HTTP client is mocked. Returns ``(provider, mock_client)``."""
    worker_mod._pending_sessions.clear()
    provider = ComdirectProvider(config=config or {})
    mock_client = AsyncMock()
    mock_client.complete_auth.return_value = True
    mock_client.get_all_data.return_value = {
        "accounts": [],
        "transactions": {},
        "depots": [],
        "depot_positions": {},
        "depot_transactions": {},
    }
    provider._client = mock_client
    # Simulate start_sync() having run.
    provider._session_identifier = "sid"
    provider._challenge_id = "cid"
    worker_mod._pending_sessions[session_id] = {
        "kind": "sync",
        "source": DataSource.COMDIRECT,
        "provider": provider,
    }
    return provider, mock_client


def test_internal_sync_complete_uses_session_overrides_with_fallbacks():
    _provider, mock_client = _make_sync_session(
        config={
            "account_transaction_limit": 1200,
            "depot_transaction_min_booking_date": "-365d",
        }
    )

    pipeline_instance = MagicMock()
    pipeline_instance.process_and_normalize.return_value = (
        MagicMock(__len__=lambda self: 0),
        "norm-run",
    )

    with (
        patch.object(worker_mod.settings, "account_transaction_limit", 500),
        patch.object(worker_mod.settings, "account_transaction_min_booking_date", None),
        patch.object(worker_mod.settings, "depot_transaction_limit", 100),
        patch.object(worker_mod.settings, "depot_transaction_min_booking_date", None),
        patch("src.normalization.ingest.ingest_canonical", return_value=0),
        patch(
            "src.normalization.pipeline.NormalizationPipeline",
            return_value=pipeline_instance,
        ),
        patch.object(
            ComdirectProvider, "post_sync_hook", AsyncMock(return_value={"depots": None})
        ),
    ):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/comdirect/complete?session_id=sess-1")

    assert resp.status_code == 200
    # Request overrides win; missing keys fall back to settings defaults.
    mock_client.get_all_data.assert_awaited_once_with(
        account_transaction_limit=1200,
        account_transaction_min_booking_date=None,
        depot_transaction_limit=100,
        depot_transaction_min_booking_date="-365d",
    )


def test_internal_sync_complete_ingests_normalizes_and_runs_hook():
    _make_sync_session()

    ingest_mock = MagicMock(return_value=5)
    pipeline_instance = MagicMock()
    pipeline_instance.process_and_normalize.return_value = (
        MagicMock(__len__=lambda self: 5),
        "norm-run",
    )

    with (
        patch("src.normalization.ingest.ingest_canonical", ingest_mock),
        patch(
            "src.normalization.pipeline.NormalizationPipeline",
            return_value=pipeline_instance,
        ),
        patch.object(
            ComdirectProvider,
            "post_sync_hook",
            AsyncMock(return_value={"depots": {"depots": 1}}),
        ),
    ):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/comdirect/complete?session_id=sess-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["ingest"]["inserted"] == 5
    assert data["ingest"]["normalized"] == 5
    assert data["ingest"]["normalize_run_id"] == "norm-run"
    # post_sync_hook output is merged into the ingest result.
    assert data["ingest"]["depots"] == {"depots": 1}
    ingest_mock.assert_called_once()
    pipeline_instance.process_and_normalize.assert_called_once()


def test_internal_sync_complete_succeeds_when_ingest_fails():
    """A persist failure is best-effort — the response is still 200 with
    ``ingest: null`` (the legacy sync contract)."""
    _make_sync_session()

    with (
        patch(
            "src.normalization.ingest.ingest_canonical",
            MagicMock(side_effect=RuntimeError("DB is down")),
        ),
        patch(
            "src.normalization.pipeline.NormalizationPipeline",
            return_value=MagicMock(),
        ),
        patch.object(
            ComdirectProvider, "post_sync_hook", AsyncMock(return_value=None)
        ),
    ):
        client = TestClient(worker_mod.app)
        resp = client.post("/internal/sync/comdirect/complete?session_id=sess-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["ingest"] is None


def test_internal_sync_complete_unknown_session_returns_404():
    worker_mod._pending_sessions.clear()
    client = TestClient(worker_mod.app)
    resp = client.post("/internal/sync/comdirect/complete?session_id=nope")
    assert resp.status_code == 404


def test_internal_sync_complete_rejects_backfill_session():
    """A backfill session must not be consumable by the sync-complete route."""
    worker_mod._pending_sessions.clear()
    worker_mod._pending_sessions["bf-1"] = {
        "kind": "backfill",
        "client": MagicMock(),
        "session_identifier": "sid",
        "challenge_id": "cid",
        "config": {},
    }
    client = TestClient(worker_mod.app)
    resp = client.post("/internal/sync/comdirect/complete?session_id=bf-1")
    assert resp.status_code == 400
