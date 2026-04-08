"""comdirect-firefly-sync — entry point."""

import asyncio
import hmac
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core.config import settings
from src.core.logging import get_logger, setup_logging
from src.scheduler.sync_job import run_sync

setup_logging()
logger = get_logger("main")

_bearer_scheme = HTTPBearer(auto_error=False)


def _require_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    token = settings.api_token
    if not token:
        return
    if not credentials or not hmac.compare_digest(credentials.credentials, token):
        raise HTTPException(status_code=401, detail="Unauthorized")


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
async def trigger_sync(_auth: None = Depends(_require_api_token)):
    """Manually trigger a sync run."""

    async def _safe_sync():
        try:
            await run_sync()
        except Exception:
            logger.exception("Sync run failed")

    asyncio.create_task(_safe_sync())
    return {"status": "triggered"}
