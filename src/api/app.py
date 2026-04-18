"""Finance API — the central API for the Personal Finance Intelligence Platform (M6).

Runs on port 8000, serves all read/write capabilities as HTTP endpoints.
No bank secrets — those stay in the worker (port 8001).
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from src.api.routers import (
    aggregates,
    auth,
    categories,
    categorization,
    depots,
    portfolio,
    reports,
    runs,
    settings as settings_router,
    sync,
    tags,
    transactions,
)
from src.core.config import Settings, settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Mark abandoned agent runs as failed on startup.

    Background tasks die with the pod they ran on. Without this, runs
    interrupted by a restart stay PENDING/RUNNING forever and block the
    UI's "no concurrent run" guard.
    """
    try:
        from src.api.deps import _get_engine
        from src.core.db.models import AgentRun, RunStatus
        from sqlalchemy.orm import Session

        with Session(_get_engine()) as s:
            stmt = (
                update(AgentRun)
                .where(AgentRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING]))
                .values(
                    status=RunStatus.FAILED,
                    finished_at=datetime.now(timezone.utc),
                    error="abandoned: pod restarted while run was active",
                )
            )
            result = s.execute(stmt)
            s.commit()
            if result.rowcount:
                logger.info("Marked %d abandoned agent run(s) as failed", result.rowcount)
    except Exception:
        logger.exception("Startup cleanup of abandoned runs failed")

    yield


def create_app() -> FastAPI:
    """Build a fresh FastAPI app.

    Refreshes the shared Settings singleton in place so tests that patch env
    before calling this factory see the updated values via deps.py.
    """
    for key, value in Settings().model_dump().items():
        setattr(settings, key, value)

    app = FastAPI(
        title="k-fin API",
        description="k-fin — Personal Finance Intelligence Platform",
        version="0.1.0",
        lifespan=_lifespan,
    )

    origins = settings.get_cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "finance-api", "platform": "k-fin"}

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(transactions.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(aggregates.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(tags.router, prefix="/api/v1")
    app.include_router(sync.router, prefix="/api/v1")
    app.include_router(settings_router.router, prefix="/api/v1")
    app.include_router(categorization.router, prefix="/api/v1")
    app.include_router(depots.router, prefix="/api/v1")
    app.include_router(portfolio.router, prefix="/api/v1")

    return app


app = create_app()
