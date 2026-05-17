"""Finance API — the central API for the Personal Finance Intelligence Platform (M6).

Runs on port 8000, serves all read/write capabilities as HTTP endpoints.
No bank secrets — those stay in the worker (port 8001).
"""

import asyncio
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
    import_csv,
    portfolio,
    reports,
    rules,
    runs,
    settings as settings_router,
    sync,
    tags,
    transactions,
)
from src.core.config import Settings, settings

logger = logging.getLogger(__name__)


def _run_refund_audit_startup() -> None:
    """Sync refund-audit pass invoked from the async lifespan hook via
    `asyncio.to_thread`. Kept as a module-level function so it can be
    swapped or unit-tested without touching the FastAPI app factory.
    """
    if not settings.database_url:
        return
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SqlSession

    from src.services.refund_audit import apply_refund_heuristic

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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Lifespan: refund-audit auto-apply pass.

    The categorization agent already auto-applies high-confidence refund
    suggestions for *new* tx as they come through. Historical erstattungen-
    rows sit unflagged in the DB — running the heuristic once at startup
    catches the unambiguous ones (Krankenkasse, Finanzamt, Booking, …) so
    the manual review queue only shows genuinely uncertain cases.
    Idempotent: a clean DB does no work.

    The work is sync (SQLAlchemy `Session`); offload to a worker thread so
    the event loop stays free for the readiness probe.
    """
    try:
        await asyncio.to_thread(_run_refund_audit_startup)
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
    # Rules router shares the /categories/* prefix space — mount it
    # before the broader categories CRUD router so the more specific
    # /categories/rules paths take precedence in the route table.
    app.include_router(rules.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(aggregates.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(tags.router, prefix="/api/v1")
    app.include_router(sync.router, prefix="/api/v1")
    app.include_router(import_csv.router, prefix="/api/v1")
    app.include_router(settings_router.router, prefix="/api/v1")
    app.include_router(categorization.router, prefix="/api/v1")
    app.include_router(depots.router, prefix="/api/v1")
    app.include_router(portfolio.router, prefix="/api/v1")
    # Dev tools — `/dev/status` is always mounted (auth-gated, used by the
    # UI to detect the environment). Destructive endpoints live on a
    # router whose `Depends(require_dev_enabled)` returns 404 when
    # settings.dev_tools_enabled is off, so they're invisible in prod.
    app.include_router(dev.status_router, prefix="/api/v1")
    app.include_router(dev.router, prefix="/api/v1")

    return app


app = create_app()
