"""Finance API — the central API for the Personal Finance Intelligence Platform (M6).

Runs on port 8000, serves all read/write capabilities as HTTP endpoints.
No bank secrets — those stay in the worker (port 8001).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    """No-op lifespan.

    Run lifecycle moved to the worker pod (M11) — `src/agents/reaper.py`
    handles heartbeat-based stale-run cleanup there, both at boot and on
    a 5-min loop. The api pod no longer drives runs, so blanket-marking
    every RUNNING row as FAILED at api startup would now incorrectly kill
    in-flight runs whenever the api rolled.
    """
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
