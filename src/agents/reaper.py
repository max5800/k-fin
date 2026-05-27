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

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from src.core.db.models import AgentRun, RunStatus
from src.core.notifier import notify_failure_from_db

logger = logging.getLogger(__name__)


class RunCancelled(Exception):
    """Raised inside an in-flight orchestrator run when its row left RUNNING.

    The user cancelled (`DELETE /runs/{id}` → `cancelled`) or the reaper
    decided the run was stale (`failed`). Either way the orchestrator
    must NOT call `_finish_run` — the row already reflects the truth.
    """


# Heartbeats are written at every batch boundary and by the orchestrator's
# watchdog while a blocking LLM call is in-flight. A 5-minute gap means the
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
    # (run_id, agent_name, error) tuples we'll fan out to the webhook
    # notifier *after* the COMMIT below — the DB write is the source of
    # truth, the webhook is opportunistic.
    notify_targets: list[tuple[str, str, str]] = []
    with Session(engine) as session:
        heartbeat_msg = "reaped: no heartbeat for >{}s".format(stale_heartbeat_s)
        # Snapshot which rows are about to be reaped so we can notify
        # by run_id after the commit. Reading first + updating second
        # is safe here because the reaper is the only writer that
        # transitions RUNNING → FAILED on this condition; a concurrent
        # orchestrator finish would race on RowVersion in either order.
        soon_reaped = session.execute(
            select(AgentRun.id, AgentRun.agent_name)
            .where(AgentRun.status == RunStatus.RUNNING)
            .where(AgentRun.heartbeat_at.is_not(None))
            .where(AgentRun.heartbeat_at < cutoff)
        ).all()
        for rid, aname in soon_reaped:
            notify_targets.append((rid, aname, heartbeat_msg))

        stmt = (
            update(AgentRun)
            .where(AgentRun.status == RunStatus.RUNNING)
            .where(AgentRun.heartbeat_at.is_not(None))
            .where(AgentRun.heartbeat_at < cutoff)
            .values(
                status=RunStatus.FAILED,
                finished_at=now,
                error=heartbeat_msg,
                last_error=None,
            )
        )
        result = session.execute(stmt)
        reaped += result.rowcount or 0

        if boot_mode:
            boot_msg = "reaped: process restarted, no heartbeat ever recorded"
            soon_reaped_boot = session.execute(
                select(AgentRun.id, AgentRun.agent_name)
                .where(AgentRun.status == RunStatus.RUNNING)
                .where(AgentRun.heartbeat_at.is_(None))
                .where(AgentRun.started_at < boot_cutoff)
            ).all()
            for rid, aname in soon_reaped_boot:
                notify_targets.append((rid, aname, boot_msg))

            stmt = (
                update(AgentRun)
                .where(AgentRun.status == RunStatus.RUNNING)
                .where(AgentRun.heartbeat_at.is_(None))
                .where(AgentRun.started_at < boot_cutoff)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=now,
                    error=boot_msg,
                    last_error=None,
                )
            )
            result = session.execute(stmt)
            reaped += result.rowcount or 0

        session.commit()

    # Webhook fan-out happens *after* the commit so a stuck Discord
    # endpoint can never block the reaper's actual job (clearing stale
    # rows). Each call is itself best-effort and never raises.
    for run_id, agent_name, err in notify_targets:
        notify_failure_from_db(
            engine,
            run_kind=f"agent:{agent_name}",
            run_id=run_id,
            error_message=err,
            occurred_at=now,
        )

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
