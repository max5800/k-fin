"""Cooperative-cancel + last_error tests for AgentOrchestrator.

The cancel path is purely cooperative: `_update_progress` reads the row's
status after every progress write, and raises `RunCancelled` if the row
left RUNNING (cancelled by user, or reaped). The outer entry points
(`run_single_for`, `run_full_for`) catch that, log, and return — they
must NOT overwrite the row.

Imports of the orchestrator are deferred to inside test functions on
purpose: importing it at module load transitively imports
`src.agents.categorization`, which captures `src.core.config.settings`.
`tests/auth/test_jwt.py` reloads the config module mid-suite and would
otherwise leave a stale singleton bound on the orchestrator side.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

# Note: AgentRun / RunStatus / RunTrigger are safe to import early — the
# models module does not capture `settings`. Only orchestrator + reaper
# (RunCancelled) get deferred.
from src.core.db.models import AgentRun, RunStatus, RunTrigger


@pytest.fixture
def RunCancelled():  # noqa: N802 — capitalised because it returns a class
    from src.agents.reaper import RunCancelled as _RunCancelled

    return _RunCancelled


@pytest.fixture
def orchestrator(db_engine):
    """An orchestrator bound to the test engine, no DB-URL connect needed."""
    from src.agents.orchestrator import AgentOrchestrator

    o = AgentOrchestrator.__new__(AgentOrchestrator)
    o.engine = db_engine
    o.own_ibans = []
    return o


def _seed_run(db_engine, *, run_id: str, status: RunStatus = RunStatus.RUNNING) -> None:
    with Session(db_engine) as s:
        s.add(
            AgentRun(
                id=run_id,
                agent_name="categorization",
                status=status,
                trigger=RunTrigger.MANUAL,
                started_at=datetime.now(timezone.utc),
            )
        )
        s.commit()


def _set_status(db_engine, run_id: str, status: RunStatus) -> None:
    with Session(db_engine) as s:
        s.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(status=status)
        )
        s.commit()


# ---------------------------------------------------------------------------
# _update_progress
# ---------------------------------------------------------------------------


class TestUpdateProgressCancellation:
    def test_raises_when_row_no_longer_running(
        self, db_engine, orchestrator, RunCancelled
    ):
        _seed_run(db_engine, run_id="cancel-1", status=RunStatus.RUNNING)
        # User flips the row to CANCELLED inline (simulating DELETE /runs/{id}).
        _set_status(db_engine, "cancel-1", RunStatus.CANCELLED)

        with pytest.raises(RunCancelled):
            orchestrator._update_progress(
                "cancel-1", current=1, total=10, message="step 1"
            )

    def test_writes_heartbeat_when_running(self, db_engine, orchestrator):
        _seed_run(db_engine, run_id="hb-1", status=RunStatus.RUNNING)

        orchestrator._update_progress(
            "hb-1", current=2, total=5, message="working"
        )

        with Session(db_engine) as s:
            row = s.get(AgentRun, "hb-1")
            assert row.heartbeat_at is not None
            assert row.progress_current == 2
            assert row.progress_total == 5
            assert row.progress_message == "working"
            # No exception raised — status is still RUNNING.
            assert row.status == RunStatus.RUNNING

    def test_does_not_raise_when_status_still_running(self, db_engine, orchestrator):
        _seed_run(db_engine, run_id="ok-1", status=RunStatus.RUNNING)
        # Should be a no-op exception-wise.
        orchestrator._update_progress("ok-1", message="still going")


# ---------------------------------------------------------------------------
# run_single_for / run_full_for swallow RunCancelled
# ---------------------------------------------------------------------------


class TestRunSingleForCancellation:
    def test_swallows_run_cancelled_and_leaves_row_untouched(
        self, db_engine, orchestrator, monkeypatch, RunCancelled
    ):
        run_id = "single-cancel-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)
        # Pre-set the row to CANCELLED with a marker so we can detect any
        # accidental overwrite.
        with Session(db_engine) as s:
            s.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=RunStatus.CANCELLED,
                    error="cancelled by user",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            s.commit()

        # Make _run_agent raise RunCancelled directly (simulating the inner
        # _update_progress observing the cancel flip).
        def _raise_cancelled(*_a, **_kw):
            raise RunCancelled("test cancel")

        monkeypatch.setattr(orchestrator, "_run_agent", _raise_cancelled)
        # _mark_running shouldn't touch our pre-set status either — but to keep
        # the test focused on the cancel path, no-op it.
        monkeypatch.setattr(orchestrator, "_mark_running", lambda *_a, **_kw: None)

        # Must NOT raise.
        orchestrator.run_single_for(run_id, "categorization")

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.CANCELLED
            assert row.error == "cancelled by user"


class TestRunFullForCancellation:
    def test_swallows_run_cancelled_and_returns_early(
        self, db_engine, orchestrator, monkeypatch, RunCancelled
    ):
        run_id = "full-cancel-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)
        with Session(db_engine) as s:
            s.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(status=RunStatus.CANCELLED, error="cancelled by user")
            )
            s.commit()

        def _raise_cancelled(*_a, **_kw):
            raise RunCancelled("test cancel")

        monkeypatch.setattr(orchestrator, "_run_all_agents", _raise_cancelled)
        monkeypatch.setattr(orchestrator, "_mark_running", lambda *_a, **_kw: None)
        # Sentinel: if the function ever calls _finish_run, the test fails.
        finish_called: list[bool] = []
        monkeypatch.setattr(
            orchestrator,
            "_finish_run",
            lambda *_a, **_kw: finish_called.append(True),
        )

        orchestrator.run_full_for(run_id)

        assert finish_called == []
        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.CANCELLED
            assert row.error == "cancelled by user"


# ---------------------------------------------------------------------------
# _record_last_error / _finish_run
# ---------------------------------------------------------------------------


class TestRecordLastError:
    def test_writes_message(self, db_engine, orchestrator):
        run_id = "err-write-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)

        orchestrator._record_last_error(run_id, "boom")

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.last_error == "boom"

    def test_clears_with_none(self, db_engine, orchestrator):
        run_id = "err-clear-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)
        orchestrator._record_last_error(run_id, "oops")

        orchestrator._record_last_error(run_id, None)

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.last_error is None

    def test_clears_with_empty_string(self, db_engine, orchestrator):
        run_id = "err-clear-2"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)
        orchestrator._record_last_error(run_id, "transient")

        orchestrator._record_last_error(run_id, "")

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.last_error is None


class TestFinishRunClearsLastError:
    def test_finish_run_always_clears_last_error(self, db_engine, orchestrator):
        run_id = "finish-clears-1"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)
        orchestrator._record_last_error(run_id, "transient retry")

        orchestrator._finish_run(run_id, results={}, status=RunStatus.SUCCEEDED)

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.SUCCEEDED
            assert row.last_error is None

    def test_finish_run_clears_last_error_on_failure(self, db_engine, orchestrator):
        run_id = "finish-clears-2"
        _seed_run(db_engine, run_id=run_id, status=RunStatus.RUNNING)
        orchestrator._record_last_error(run_id, "still hot")

        orchestrator._finish_run(
            run_id, results={}, error="final boom", status=RunStatus.FAILED
        )

        with Session(db_engine) as s:
            row = s.get(AgentRun, run_id)
            assert row.status == RunStatus.FAILED
            assert row.error == "final boom"
            assert row.last_error is None
