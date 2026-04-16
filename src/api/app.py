"""Finance API — the central API for the Personal Finance Intelligence Platform (M6).

Runs on port 8000, serves all read/write capabilities as HTTP endpoints.
No bank secrets — those stay in the worker (port 8001).
"""

from fastapi import FastAPI

from src.api.routers import categories, transactions

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
