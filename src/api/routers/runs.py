"""Runs router — trigger and monitor agent runs (M7).

Run execution lives in the worker pod (port 8001) — the api just creates
the AgentRun row and fire-and-forgets a POST to the worker. This keeps
long-running LLM work out of the api request-handler process so api
rollouts don't kill in-flight runs (the worker's reaper still cleans up
if the worker itself dies mid-run).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import (
    RunCreate,
    RunHealthOut,
    RunHealthPendingSource,
    RunListOut,
    RunOut,
)
from src.core.config import settings
from src.core.db.models import (
    AgentRun,
    AppSettings,
    NormalizedTransaction,
    RunStatus,
    RunTrigger,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    dependencies=[Depends(require_token)],
)

KNOWN_AGENTS = {
    "categorization",
    "category_audit",
    "budget_analysis",
    "weekly_analysis",
    "monthly_analysis",
    "anomaly",
    "synthesis",
}

# Short timeout — the worker accepts the request, schedules the background
# task, and returns 202 within milliseconds. Anything slower is a worker
# health issue we want surfaced to the caller.
_DISPATCH_TIMEOUT_S = 5.0
_DEFAULT_AUTO_APPLY_CONFIDENCE = 0.60


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
    period_days: int | None = Query(
        None,
        ge=1,
        le=3650,
        description=(
            "Optional override for the agent's built-in time window. "
            "Forwarded to weekly, monthly, and anomaly agents; "
            "categorization and synthesis ignore it."
        ),
    ),
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

    payload: dict = {"run_id": run.id}
    if period_days is not None:
        payload["period_days"] = period_days
    try:
        _dispatch_to_worker("/internal/runs/start-full", payload)
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
    period_days: int | None = Query(
        None,
        ge=1,
        le=3650,
        description=(
            "Optional override for the agent's built-in time window. "
            "Forwarded to weekly, monthly, and anomaly agents; "
            "categorization and synthesis ignore it."
        ),
    ),
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

    payload: dict = {"run_id": run.id}
    if period_days is not None:
        payload["period_days"] = period_days
    try:
        _dispatch_to_worker(
            f"/internal/runs/start?agent_name={agent_name}",
            payload,
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


@router.post(
    "/{run_id}/rerun",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Re-trigger a failed or cancelled agent run",
)
def rerun_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    """Spawn a fresh AgentRun mirroring the original's `agent_name`.

    Only allowed for terminal-but-incomplete runs (`failed`, `cancelled`) —
    rerunning a `succeeded` run would be wasted spend, and rerunning a
    `pending`/`running` row would race the worker.

    The pipeline is idempotent by design: the categorization agent only
    queries `category_id IS NULL`, and the synthesis agents are cheap to
    re-execute. Already-categorized transactions are not touched.
    """
    original = db.get(AgentRun, run_id)
    if original is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        )

    if original.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"Run is {original.status.value}; only failed or cancelled runs can be rerun"),
        )

    agent_name = original.agent_name
    new_run = AgentRun(
        id=uuid.uuid4().hex,
        agent_name=agent_name,
        status=RunStatus.PENDING,
        trigger=RunTrigger.MANUAL,
        started_at=datetime.now(timezone.utc),
    )
    db.add(new_run)
    db.commit()
    db.refresh(new_run)

    # Match the dispatch path of the original start endpoint — the worker
    # has separate routes for the full pipeline vs. a single agent.
    if agent_name == "full":
        dispatch_path = "/internal/runs/start-full"
    else:
        dispatch_path = f"/internal/runs/start?agent_name={agent_name}"

    try:
        _dispatch_to_worker(dispatch_path, {"run_id": new_run.id})
    except HTTPException:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == new_run.id)
            .values(
                status=RunStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error="Worker dispatch failed",
            )
        )
        db.commit()
        raise

    return new_run


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

    return RunListOut(
        items=[RunOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


def _load_auto_apply_threshold(db: Session) -> float:
    row = db.get(AppSettings, 1)
    if row is None:
        return _DEFAULT_AUTO_APPLY_CONFIDENCE
    return float(row.auto_apply_confidence)


def _categorization_result_blob(run: AgentRun) -> dict | None:
    if not isinstance(run.result, dict):
        return None
    nested = run.result.get("categorization")
    if isinstance(nested, dict):
        return nested
    if run.agent_name == "categorization":
        return run.result
    return None


def _categorization_memory_blob(run: AgentRun) -> dict:
    if not isinstance(run.usage_detail, dict):
        return {}
    detail = run.usage_detail.get("categorization")
    if not isinstance(detail, dict):
        return {}
    memory = detail.get("memory")
    return memory if isinstance(memory, dict) else {}


def _suggestion_confidence(suggestion: dict) -> float | None:
    try:
        value = float(suggestion.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 1:
        return None
    return value


def _source_value(source: object) -> str:
    return str(getattr(source, "value", source))


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


@router.get("/health", response_model=RunHealthOut)
def get_runs_health(
    db: Session = Depends(get_db),
    window_days: int = Query(7, ge=1, le=90),
) -> RunHealthOut:
    threshold = _load_auto_apply_threshold(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    rows = (
        db.execute(
            select(AgentRun)
            .where(AgentRun.status == RunStatus.SUCCEEDED)
            .where(AgentRun.agent_name.in_(["categorization", "full"]))
            .where(AgentRun.started_at >= cutoff)
            .order_by(AgentRun.started_at.desc())
        )
        .scalars()
        .all()
    )

    runs_total = 0
    confidences: list[float] = []
    suggestions_total = 0
    high_confidence_total = 0
    memory_batches_total = 0
    memory_batches_with_hits = 0
    memory_hits_total = 0
    low_conf_with_memory = 0
    low_conf_without_memory = 0

    for run in rows:
        blob = _categorization_result_blob(run)
        if blob is None:
            continue
        runs_total += 1

        suggestions = blob.get("suggestions", [])
        if not isinstance(suggestions, list):
            suggestions = []
        run_confidences = [
            confidence
            for suggestion in suggestions
            if isinstance(suggestion, dict)
            for confidence in [_suggestion_confidence(suggestion)]
            if confidence is not None
        ]
        confidences.extend(run_confidences)
        suggestions_total += len(run_confidences)

        persisted_high_conf = blob.get("high_confidence_count")
        if isinstance(persisted_high_conf, int):
            high_confidence_total += min(
                max(0, persisted_high_conf),
                len(run_confidences),
            )
        else:
            high_confidence_total += sum(
                1 for confidence in run_confidences if confidence >= threshold
            )

        memory = _categorization_memory_blob(run)
        hits_per_batch = memory.get("hits_per_batch", [])
        if not isinstance(hits_per_batch, list):
            hits_per_batch = []
        hit_counts = [
            int(hit) for hit in hits_per_batch if isinstance(hit, int | float) and hit >= 0
        ]
        batches_total = memory.get("batches_total")
        batches_total_int = (
            max(0, int(batches_total))
            if isinstance(batches_total, int | float)
            else len(hit_counts)
        )
        batches_with_hits = memory.get("batches_with_memory")
        batches_with_hits_int = (
            max(0, int(batches_with_hits))
            if isinstance(batches_with_hits, int | float)
            else sum(1 for hit in hit_counts if hit > 0)
        )

        memory_batches_total += batches_total_int
        memory_batches_with_hits += min(batches_with_hits_int, batches_total_int)
        memory_hits_total += sum(hit_counts)
        low_conf_with_memory += _nonnegative_int(memory.get("low_conf_with_memory"))
        low_conf_without_memory += _nonnegative_int(memory.get("low_conf_without_memory"))

    pending_rows = db.execute(
        select(NormalizedTransaction.source, func.count())
        .where(NormalizedTransaction.category_id.is_(None))
        .where(NormalizedTransaction.internal_transfer.is_(False))
        .group_by(NormalizedTransaction.source)
        .order_by(NormalizedTransaction.source)
    ).all()
    pending_by_source = [
        RunHealthPendingSource(source=_source_value(source), pending=count)
        for source, count in pending_rows
    ]
    pending_total = sum(item.pending for item in pending_by_source)

    return RunHealthOut(
        window_days=window_days,
        threshold=threshold,
        runs_total=runs_total,
        suggestions_total=suggestions_total,
        high_confidence_total=high_confidence_total,
        auto_apply_rate=(
            high_confidence_total / suggestions_total if suggestions_total > 0 else None
        ),
        avg_confidence=(sum(confidences) / len(confidences) if confidences else None),
        memory_batches_total=memory_batches_total,
        memory_batches_with_hits=memory_batches_with_hits,
        memory_hit_rate=(
            memory_batches_with_hits / memory_batches_total if memory_batches_total > 0 else None
        ),
        memory_hits_total=memory_hits_total,
        low_conf_with_memory=low_conf_with_memory,
        low_conf_without_memory=low_conf_without_memory,
        pending_by_source=pending_by_source,
        pending_total=pending_total,
    )


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
