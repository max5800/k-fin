"""Runs router — trigger and monitor agent runs (M6)."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import RunCreate, RunListOut, RunOut
from src.core.db.models import AgentRun, RunStatus, RunTrigger

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


@router.post("/{agent_name}", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def start_run(
    agent_name: str,
    body: RunCreate | None = None,
    db: Session = Depends(get_db),
):
    if agent_name not in KNOWN_AGENTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown agent '{agent_name}'",
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
