"""comdirect-worker — internal sync service.

Runs on port 8001, never exposed to the internet.
Receives sync requests from the public comdirect-api service.
"""

import asyncio

from fastapi import FastAPI

from src.core.logging import get_logger, setup_logging
from src.scheduler.sync_job import run_sync

setup_logging()
logger = get_logger("worker")

_background_tasks: set[asyncio.Task] = set()

app = FastAPI(
    title="comdirect-worker",
    description="Internal sync worker — not publicly accessible",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "comdirect-worker"}


@app.post("/internal/sync")
async def internal_sync():
    """Run a sync job. Called by the public API service."""

    async def _safe_sync():
        try:
            await run_sync()
        except Exception:
            logger.exception("Sync run failed")

    task = asyncio.create_task(_safe_sync())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "triggered"}
