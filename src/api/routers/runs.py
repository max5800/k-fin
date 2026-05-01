"""Runs router — trigger and monitor agent runs (M7).

Run execution lives in the worker pod (port 8001) — the api just creates
the AgentRun row and fire-and-forgets a POST to the worker. This keeps
long-running LLM work out of the api request-handler process so api
rollouts don't kill in-flight runs (the worker's reaper still cleans up
if the worker itself dies mid-run).
"""

import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import RunCreate, RunListOut, RunOut
from src.core.config import settings
from src.core.db.models import AgentRun, RunStatus, RunTrigger

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    dependencies=[Depends(require_token)],
)

KNOWN_AGENTS = {
    "categorization",
    "weekly_analysis",
    "monthly_analysis",
    "anomaly",
    "synthesis",
}

# Short timeout — the worker accepts the request, schedules the background
# task, and returns 202 within milliseconds. Anything slower is a worker
# health issue we want surfaced to the caller.
_DISPATCH_TIMEOUT_S = 5.0


def _dispatch_to_worker(path: str, payload: dict) -> None:
    """Fire a single POST at the worker. Raises HTTPException on failure."""
    url = settings.worker_url.rstrip("/") + path
    try:
        resp = httpx.post(url, json=payload, timeout=_DISPATCH_TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.error("Worker dispatch %s failed: %s", path, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Worker unreachable: {exc}",
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Worker rejected dispatch ({resp.status_code}): {resp.text[:200]}",
        )


@router.post(
    "/full",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a full agent pipeline run",
)
def start_full_run(
    body: RunCreate | None = None,
    db: Session = Depends(get_db),
):
    trigger = RunTrigger(body.trigger) if body else RunTrigger.MANUAL

    run = AgentRun(
        id=uuid.uuid4().hex,
        agent_name="full",
        status=RunStatus.PENDING,
        trigger=trigger,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        _dispatch_to_worker("/internal/runs/start-full", {"run_id": run.id})
    except HTTPException:
        # Mark the row as failed so the user sees the dispatch error
        # instead of a phantom RUNNING that the worker never picks up.
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(
                status=RunStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error="Worker dispatch failed",
            )
        )
        db.commit()
        raise

    return run


@router.post(
    "/{agent_name}",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a single agent run",
)
def start_run(
    agent_name: str,
    body: RunCreate | None = None,
    db: Session = Depends(get_db),
):
    if agent_name not in KNOWN_AGENTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown agent '{agent_name}'. Valid: {', '.join(sorted(KNOWN_AGENTS))}",
        )

    trigger = RunTrigger(body.trigger) if body else RunTrigger.MANUAL

    run = AgentRun(
        id=uuid.uuid4().hex,
        agent_name=agent_name,
        status=RunStatus.PENDING,
        trigger=trigger,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        _dispatch_to_worker(
            f"/internal/runs/start?agent_name={agent_name}",
            {"run_id": run.id},
        )
    except HTTPException:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(
                status=RunStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error="Worker dispatch failed",
            )
        )
        db.commit()
        raise

    return run


@router.get("", response_model=RunListOut)
def list_runs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    agent_name: str | None = Query(None),
    run_status: str | None = Query(None, alias="status"),
):
    stmt = select(AgentRun)
    count_stmt = select(func.count()).select_from(AgentRun)

    if agent_name:
        stmt = stmt.where(AgentRun.agent_name == agent_name)
        count_stmt = count_stmt.where(AgentRun.agent_name == agent_name)
    if run_status:
        stmt = stmt.where(AgentRun.status == run_status)
        count_stmt = count_stmt.where(AgentRun.status == run_status)

    total = db.execute(count_stmt).scalar_one()

    stmt = stmt.order_by(AgentRun.started_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()

    return RunListOut(items=[RunOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunOut)
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    run = db.get(AgentRun, run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.delete(
    "/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a running agent run",
)
def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
) -> None:
    """Cooperative cancel: flip the row to `cancelled`. The worker's
    `_update_progress` checks the status at every batch boundary and
    raises `RunCancelled` on the next call, ending the in-flight work.

    Idempotent on already-terminal runs returns 409 (so the UI can show
    a meaningful error if the user clicks twice).
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    if run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run is already {run.status.value}",
        )

    db.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id)
        .values(
            status=RunStatus.CANCELLED,
            finished_at=datetime.now(timezone.utc),
            error="cancelled by user",
            last_error=None,
        )
    )
    db.commit()
    return None
