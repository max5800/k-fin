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
