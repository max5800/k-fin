"""Tests for the stale-run reaper.

The reaper marks orphaned `agent_runs` as FAILED so the UI doesn't
show 8-hour-stale "running" rows after a worker dies. Two modes:

- normal: only stale-heartbeat RUNNING rows.
- boot_mode=True: ALSO catches RUNNING rows with NULL heartbeat older
  than the boot-grace window (rows from a process that never wrote
  heartbeats).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from src.agents.reaper import reap_stale_runs
from src.core.db.models import AgentRun, RunStatus, RunTrigger


def _add_run(
    session: Session,
    *,
    run_id: str,
    status: RunStatus,
    started_at: datetime,
    heartbeat_at: datetime | None,
    finished_at: datetime | None = None,
) -> None:
    session.add(
        AgentRun(
            id=run_id,
            agent_name="categorization",
            status=status,
            trigger=RunTrigger.MANUAL,
            started_at=started_at,
            finished_at=finished_at,
            heartbeat_at=heartbeat_at,
        )
    )


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


def test_reaps_stale_heartbeat_running_rows(db_engine, now):
    """A RUNNING row with a heartbeat >stale_heartbeat_s ago becomes FAILED."""
    with Session(db_engine) as s:
        _add_run(
            s,
            run_id="stale-1",
            status=RunStatus.RUNNING,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=600),
        )
        s.commit()

    reaped = reap_stale_runs(db_engine, stale_heartbeat_s=300, boot_mode=False)
    assert reaped == 1

    with Session(db_engine) as s:
        row = s.get(AgentRun, "stale-1")
        assert row.status == RunStatus.FAILED
        assert row.finished_at is not None
        assert row.error is not None
        assert row.error.startswith("reaped:")
        assert row.last_error is None


def test_boot_mode_reaps_null_heartbeat_rows(db_engine, now):
    """boot_mode=True catches RUNNING rows with NULL heartbeat older than boot_grace."""
    with Session(db_engine) as s:
        _add_run(
            s,
            run_id="null-hb-old",
            status=RunStatus.RUNNING,
            started_at=now - timedelta(seconds=1200),
            heartbeat_at=None,
        )
        # Within grace window — must NOT be reaped.
        _add_run(
            s,
            run_id="null-hb-fresh",
            status=RunStatus.RUNNING,
            started_at=now - timedelta(seconds=60),
            heartbeat_at=None,
        )
        # Stale heartbeat — caught regardless of boot_mode.
        _add_run(
            s,
            run_id="stale-hb",
            status=RunStatus.RUNNING,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=900),
        )
        s.commit()

    reaped = reap_stale_runs(
        db_engine, stale_heartbeat_s=300, boot_grace_s=600, boot_mode=True
    )
    assert reaped == 2

    with Session(db_engine) as s:
        assert s.get(AgentRun, "null-hb-old").status == RunStatus.FAILED
        assert s.get(AgentRun, "null-hb-fresh").status == RunStatus.RUNNING
        assert s.get(AgentRun, "stale-hb").status == RunStatus.FAILED


def test_non_boot_mode_ignores_null_heartbeat_rows(db_engine, now):
    """Without boot_mode, NULL-heartbeat rows are left alone."""
    with Session(db_engine) as s:
        _add_run(
            s,
            run_id="null-hb-ancient",
            status=RunStatus.RUNNING,
            started_at=now - timedelta(hours=24),
            heartbeat_at=None,
        )
        s.commit()

    reaped = reap_stale_runs(db_engine, stale_heartbeat_s=300, boot_mode=False)
    assert reaped == 0

    with Session(db_engine) as s:
        assert s.get(AgentRun, "null-hb-ancient").status == RunStatus.RUNNING


def test_does_not_touch_terminal_rows(db_engine, now):
    """SUCCEEDED, FAILED, CANCELLED rows are never touched, even with stale heartbeats."""
    with Session(db_engine) as s:
        _add_run(
            s,
            run_id="succeeded",
            status=RunStatus.SUCCEEDED,
            started_at=now - timedelta(hours=2),
            heartbeat_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
        )
        _add_run(
            s,
            run_id="failed",
            status=RunStatus.FAILED,
            started_at=now - timedelta(hours=2),
            heartbeat_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
        )
        _add_run(
            s,
            run_id="cancelled",
            status=RunStatus.CANCELLED,
            started_at=now - timedelta(hours=2),
            heartbeat_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
        )
        s.commit()

    reaped = reap_stale_runs(
        db_engine, stale_heartbeat_s=300, boot_grace_s=60, boot_mode=True
    )
    assert reaped == 0

    with Session(db_engine) as s:
        assert s.get(AgentRun, "succeeded").status == RunStatus.SUCCEEDED
        assert s.get(AgentRun, "failed").status == RunStatus.FAILED
        assert s.get(AgentRun, "cancelled").status == RunStatus.CANCELLED


def test_returns_zero_when_nothing_to_reap(db_engine, now):
    """A clean DB returns 0 — no errors, no false positives."""
    assert reap_stale_runs(db_engine, boot_mode=True) == 0
    assert reap_stale_runs(db_engine, boot_mode=False) == 0


def test_error_message_mentions_threshold(db_engine, now):
    """Reaped row's `error` includes the threshold so operators can self-debug."""
    with Session(db_engine) as s:
        _add_run(
            s,
            run_id="stale",
            status=RunStatus.RUNNING,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=900),
        )
        s.commit()

    reap_stale_runs(db_engine, stale_heartbeat_s=300, boot_mode=False)

    with Session(db_engine) as s:
        row = s.get(AgentRun, "stale")
        assert "300" in row.error
