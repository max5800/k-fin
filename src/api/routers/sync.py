"""Sync proxy — forwards sync requests to the internal worker service."""

import httpx
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import Auth
from src.core.config import settings

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Auth])


class SyncStartRequest(BaseModel):
    account_transaction_limit: int | None = Field(default=None, gt=0)
    account_transaction_min_booking_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$|^-\d+d$"
    )
    depot_transaction_limit: int | None = Field(default=None, gt=0)
    depot_transaction_min_booking_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$|^-\d+d$"
    )


@router.post("/start")
async def sync_start(payload: SyncStartRequest | None = Body(default=None)):
    """Begin a sync run — triggers pushTAN challenge via the internal worker."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.worker_url}/internal/sync/start",
                json=(payload.model_dump(exclude_none=True) if payload else None),
            )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")


@router.post("/normalize")
async def normalize():
    """Re-run normalization pipeline over existing raw_transactions."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{settings.worker_url}/internal/normalize",
            )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")


@router.post("/confirm")
async def sync_confirm(session_id: str):
    """Confirm TAN and complete sync via the internal worker."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{settings.worker_url}/internal/sync/confirm",
                params={"session_id": session_id},
            )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")


# ---------------------------------------------------------------------------
# Historical backfill — same TAN-in-the-loop pattern as the sync flow.
# ---------------------------------------------------------------------------


class BackfillStartRequest(BaseModel):
    months: int | None = Field(default=None, ge=1, le=24)


def _safe_json_detail(resp: httpx.Response) -> str:
    """Extract a useful error message from a worker response.

    The worker may return non-JSON on 500s (e.g. FastAPI's default HTML
    page). Fall back to status reason + truncated body so the UI sees
    something actionable instead of a JSONDecodeError.
    """
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except ValueError:
        text = (resp.text or "").strip()
        return f"Worker returned HTTP {resp.status_code}: {text[:200]}" if text else (
            f"Worker returned HTTP {resp.status_code}"
        )


@router.post("/backfill/start")
async def backfill_start(payload: BackfillStartRequest | None = Body(default=None)):
    """Begin a historical-backfill run — triggers pushTAN via the worker.

    Returns 409 if a backfill is already running.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.worker_url}/internal/backfill/start",
                json=(payload.model_dump(exclude_none=True) if payload else None),
            )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=_safe_json_detail(resp),
                )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")


@router.post("/backfill/confirm")
async def backfill_confirm(session_id: str):
    """Confirm TAN and dispatch the backfill walker via the worker."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.worker_url}/internal/backfill/confirm",
                params={"session_id": session_id},
            )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=_safe_json_detail(resp),
                )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")


@router.get("/backfill/runs/{run_id}")
async def backfill_run_status(run_id: str):
    """Poll the status of a backfill run for UI progress display."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.worker_url}/internal/backfill/runs/{run_id}",
            )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=_safe_json_detail(resp),
                )
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Worker service unreachable")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Worker communication failed: {e}")
