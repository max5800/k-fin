"""Stale-run reaper.

Marks orphaned `agent_runs` rows as `failed` so the UI doesn't render an
8-hour stale "running" badge after a process death. Two entry points:

- `reap_stale_runs()` — one-shot UPDATE used at worker startup and as the
  body of the periodic loop.
- `start_periodic_reaper()` — fires an asyncio task that calls the one-shot
  every `interval_s` until the worker exits.

Cancellation contract: the orchestrator periodically re-reads its own row
inside `_update_progress`. If the status is no longer `running`, it raises
`RunCancelled` and the orchestrator's outer except path leaves the row
untouched (so reap-set `failed` / user-set `cancelled` survive).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from src.core.db.models import AgentRun, RunStatus

logger = logging.getLogger(__name__)


class RunCancelled(Exception):
    """Raised inside an in-flight orchestrator run when its row left RUNNING.

    The user cancelled (`DELETE /runs/{id}` → `cancelled`) or the reaper
    decided the run was stale (`failed`). Either way the orchestrator
    must NOT call `_finish_run` — the row already reflects the truth.
    """


# Heartbeats are written at every batch boundary. A 5-minute gap means the
# process is either dead or genuinely stalled — both warrant reaping.
DEFAULT_STALE_HEARTBEAT_S = 300

# Boot-time: also reap RUNNING rows whose heartbeat_at is NULL (rows that
# pre-date M11 reliability work) older than this. Prevents unbounded
# leftover state from blocking new runs.
BOOT_GRACE_S = 600


def reap_stale_runs(
    engine: Engine,
    *,
    stale_heartbeat_s: int = DEFAULT_STALE_HEARTBEAT_S,
    boot_grace_s: int = BOOT_GRACE_S,
    boot_mode: bool = False,
) -> int:
    """Mark stale RUNNING rows as FAILED. Returns count of reaped rows.

    `boot_mode=True` also catches rows with `heartbeat_at IS NULL` whose
    `started_at` is older than `boot_grace_s` — needed once at startup
    because the previous process never wrote a heartbeat.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=stale_heartbeat_s)
    boot_cutoff = now - timedelta(seconds=boot_grace_s)

    reaped = 0
    with Session(engine) as session:
        stmt = (
            update(AgentRun)
            .where(AgentRun.status == RunStatus.RUNNING)
            .where(AgentRun.heartbeat_at.is_not(None))
            .where(AgentRun.heartbeat_at < cutoff)
            .values(
                status=RunStatus.FAILED,
                finished_at=now,
                error="reaped: no heartbeat for >{}s".format(stale_heartbeat_s),
                last_error=None,
            )
        )
        result = session.execute(stmt)
        reaped += result.rowcount or 0

        if boot_mode:
            stmt = (
                update(AgentRun)
                .where(AgentRun.status == RunStatus.RUNNING)
                .where(AgentRun.heartbeat_at.is_(None))
                .where(AgentRun.started_at < boot_cutoff)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=now,
                    error="reaped: process restarted, no heartbeat ever recorded",
                    last_error=None,
                )
            )
            result = session.execute(stmt)
            reaped += result.rowcount or 0

        session.commit()

    if reaped:
        logger.warning("Reaper marked %d stale run(s) as failed", reaped)
    else:
        logger.debug("Reaper: no stale runs")
    return reaped


async def start_periodic_reaper(
    engine: Engine,
    *,
    interval_s: int = 300,
    stale_heartbeat_s: int = DEFAULT_STALE_HEARTBEAT_S,
) -> asyncio.Task:
    """Fire-and-forget background loop. Caller owns the returned Task."""

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                await asyncio.to_thread(
                    reap_stale_runs,
                    engine,
                    stale_heartbeat_s=stale_heartbeat_s,
                    boot_mode=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reaper loop iteration failed; will retry")

    return asyncio.create_task(_loop(), name="agent-reaper")
