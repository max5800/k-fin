"""comdirect-firefly-sync — entry point."""

import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from src.core.config import settings
from src.core.logging import get_logger, setup_logging
from src.scheduler.sync_job import run_sync

setup_logging()
logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_sync,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="comdirect_sync",
        name="Comdirect → Firefly III sync",
    )
    scheduler.start()
    logger.info(
        f"🏦 comdirect-firefly-sync started — syncing every {settings.sync_interval_minutes}m"
    )
    yield
    scheduler.shutdown()


app = FastAPI(
    title="comdirect-firefly-sync",
    description="Read-only sync from Comdirect to Firefly III",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "comdirect-firefly-sync"}


@app.post("/sync/trigger")
async def trigger_sync():
    """Manually trigger a sync run."""
    asyncio.create_task(run_sync())
    return {"status": "triggered"}
