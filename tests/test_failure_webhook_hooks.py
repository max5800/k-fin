"""Integration tests for the sync/agent failure → webhook hook.

Verifies that:

- The reaper fans out a webhook call per stale row when ``webhook_url``
  is set and stays silent when it's null.
- The orchestrator's ``_finish_run`` fires a webhook on FAILED status
  and stays silent on SUCCEEDED.
- The webhook never fires when no webhook_url is configured.
- A failing webhook never blocks the underlying terminal status write.

httpx is mocked end-to-end — no Comdirect, no Discord, no real network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
from sqlalchemy.orm import Session

from src.agents.orchestrator import AgentOrchestrator
from src.agents.reaper import reap_stale_runs
from src.core.db.models import AgentRun, AppSettings, RunStatus, RunTrigger

WEBHOOK = "https://discord.com/api/webhooks/123/abc-token"


def _seed_webhook(db_session: Session, url: str | None) -> None:
    db_session.add(
        AppSettings(
            id=1,
            auto_apply_confidence=Decimal("0.60"),
            page_size=25,
            webhook_url=url,
        )
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# Reaper failure-hook
# ---------------------------------------------------------------------------


def test_reaper_fires_webhook_when_url_set(db_engine, db_session):
    _seed_webhook(db_session, WEBHOOK)
    now = datetime.now(timezone.utc)
    db_session.add(
        AgentRun(
            id="stale-r1",
            agent_name="categorization",
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=600),
        )
    )
    db_session.commit()

    with patch("src.core.notifier.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=204)
        reaped = reap_stale_runs(db_engine, stale_heartbeat_s=300, boot_mode=False)

    assert reaped == 1
    assert mock_post.call_count == 1
    # The agent_name flows through into the run_kind label.
    sent_json = mock_post.call_args.kwargs["json"]
    assert "categorization" in sent_json["content"]


def test_reaper_silent_when_no_webhook(db_engine, db_session):
    _seed_webhook(db_session, None)
    now = datetime.now(timezone.utc)
    db_session.add(
        AgentRun(
            id="stale-r2",
            agent_name="weekly_analysis",
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=600),
        )
    )
    db_session.commit()

    with patch("src.core.notifier.httpx.post") as mock_post:
        reaped = reap_stale_runs(db_engine, stale_heartbeat_s=300, boot_mode=False)

    assert reaped == 1
    assert mock_post.call_count == 0


def test_reaper_continues_when_webhook_fails(db_engine, db_session):
    """Webhook timeout must not roll back the FAILED-status update."""
    _seed_webhook(db_session, WEBHOOK)
    now = datetime.now(timezone.utc)
    db_session.add(
        AgentRun(
            id="stale-r3",
            agent_name="anomaly",
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=600),
        )
    )
    db_session.commit()

    with patch("src.core.notifier.httpx.post") as mock_post:
        mock_post.side_effect = httpx.TimeoutException("slow")
        reaped = reap_stale_runs(db_engine, stale_heartbeat_s=300, boot_mode=False)

    assert reaped == 1
    with Session(db_engine) as s:
        row = s.get(AgentRun, "stale-r3")
        # Critical: the DB write happened *before* the webhook attempt.
        assert row.status == RunStatus.FAILED


# ---------------------------------------------------------------------------
# Orchestrator _finish_run failure hook
# ---------------------------------------------------------------------------


def test_orchestrator_fires_webhook_on_failed_finish(db_engine, db_session):
    _seed_webhook(db_session, WEBHOOK)
    db_session.add(
        AgentRun(
            id="orc-fail-1",
            agent_name="weekly_analysis",
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
    )
    db_session.commit()

    db_url = db_engine.url.render_as_string(hide_password=False)
    orchestrator = AgentOrchestrator(database_url=db_url)

    with patch("src.core.notifier.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=204)
        orchestrator._finish_run(
            "orc-fail-1",
            results={},
            error="something broke for DE12345678901234567890",
            status=RunStatus.FAILED,
        )

    assert mock_post.call_count == 1
    sent_json = mock_post.call_args.kwargs["json"]
    rendered = str(sent_json)
    # IBAN masking enforced end-to-end.
    import re

    assert re.search(r"\bDE\d{20}\b", rendered) is None
    # Run kind reflects the agent.
    assert "weekly_analysis" in sent_json["content"]


def test_orchestrator_no_webhook_on_success(db_engine, db_session):
    _seed_webhook(db_session, WEBHOOK)
    db_session.add(
        AgentRun(
            id="orc-ok-1",
            agent_name="weekly_analysis",
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
    )
    db_session.commit()

    db_url = db_engine.url.render_as_string(hide_password=False)
    orchestrator = AgentOrchestrator(database_url=db_url)

    with patch("src.core.notifier.httpx.post") as mock_post:
        orchestrator._finish_run(
            "orc-ok-1",
            results={"weekly_analysis": {"period": "2026-W18"}},
            status=RunStatus.SUCCEEDED,
        )

    assert mock_post.call_count == 0


def test_orchestrator_no_webhook_when_url_null(db_engine, db_session):
    _seed_webhook(db_session, None)
    db_session.add(
        AgentRun(
            id="orc-fail-2",
            agent_name="weekly_analysis",
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
    )
    db_session.commit()

    db_url = db_engine.url.render_as_string(hide_password=False)
    orchestrator = AgentOrchestrator(database_url=db_url)

    with patch("src.core.notifier.httpx.post") as mock_post:
        orchestrator._finish_run(
            "orc-fail-2",
            results={},
            error="boom",
            status=RunStatus.FAILED,
        )

    assert mock_post.call_count == 0
