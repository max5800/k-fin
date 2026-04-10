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

app = FastAPI(
    title="comdirect-worker",
    description="Internal sync worker — not publicly accessible",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# In-memory sync state (single-worker deployment)
# ---------------------------------------------------------------------------

_sync_state: dict = {"status": "idle", "error": None}
_sync_task: asyncio.Task | None = None


@app.get("/health")
def health():
    return {"status": "ok", "service": "comdirect-worker"}


@app.post("/internal/sync")
async def internal_sync():
    """Run a sync job. Called by the public API service."""
    global _sync_task

    if _sync_task and not _sync_task.done():
        return {"status": _sync_state["status"], "message": "Sync already in progress"}

    _sync_state["status"] = "pending_tan"
    _sync_state["error"] = None

    async def _run():
        try:
            _sync_state["status"] = "pending_tan"
            await run_sync()
            _sync_state["status"] = "done"
        except Exception as exc:
            logger.exception("Sync run failed")
            _sync_state["status"] = "error"
            _sync_state["error"] = str(exc)

    _sync_task = asyncio.create_task(_run())
    return {"status": "pending_tan", "message": "TAN sent to device, confirm in app"}


@app.get("/internal/sync/status")
async def sync_status():
    """Return current sync state."""
    return {"status": _sync_state["status"], "error": _sync_state.get("error")}
