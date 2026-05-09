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
    dev,
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
    """Lifespan: refund-audit auto-apply pass.

    The categorization agent already auto-applies high-confidence refund
    suggestions for *new* tx as they come through. Historical erstattungen-
    rows (pre-2026-05-08) sit unflagged in the DB — running the heuristic
    once at startup catches the unambiguous ones (Krankenkasse, Finanzamt,
    Booking, …) so the manual review queue only shows genuinely uncertain
    cases. Idempotent: a clean DB does no work.

    Run-lifecycle cleanup stays in the worker (`src/agents/reaper.py`).
    """
    try:
        if settings.database_url:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import Session as SqlSession

            from src.api.routers.aggregates import apply_refund_heuristic

            engine = create_engine(settings.database_url)
            try:
                with SqlSession(engine) as session:
                    counts = apply_refund_heuristic(session)
                if any(counts.values()):
                    logger.info(
                        "refund_audit auto-apply: refund=%d income=%d review=%d",
                        counts["applied_refund"],
                        counts["applied_income"],
                        counts["left_for_review"],
                    )
            finally:
                engine.dispose()
    except Exception:  # noqa: BLE001 — never block API startup on cleanup
        logger.exception("refund_audit auto-apply skipped on startup")
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
    # Dev router is always mounted so the UI can call /dev/status to detect
    # the environment. Destructive sub-endpoints (/wipe, /seed) self-guard
    # via `_require_enabled` and 404 when settings.dev_tools_enabled is off.
    app.include_router(dev.router, prefix="/api/v1")

    return app


app = create_app()
