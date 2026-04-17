"""Finance API — the central API for the Personal Finance Intelligence Platform (M6).

Runs on port 8000, serves all read/write capabilities as HTTP endpoints.
No bank secrets — those stay in the worker (port 8001).
"""

from fastapi import FastAPI

from src.api.routers import aggregates, categories, reports, runs, sync, tags, transactions
from src.core.config import Settings, settings


def create_app() -> FastAPI:
    """Build a fresh FastAPI app.

    Refreshes the shared Settings singleton in place so tests that patch env
    before calling this factory see the updated values via deps.py.
    """
    for key, value in Settings().model_dump().items():
        setattr(settings, key, value)

    app = FastAPI(
        title="K-Fin API",
        description="K-Fin — Personal Finance Intelligence Platform",
        version="0.1.0",
    )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "finance-api"}

    app.include_router(transactions.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(aggregates.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(tags.router, prefix="/api/v1")
    app.include_router(sync.router, prefix="/api/v1")

    return app


app = create_app()
