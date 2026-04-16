"""Database engine, session factory, and FastAPI dependency.

Synchronous SQLAlchemy — FastAPI runs sync dependencies in its
threadpool, which is fine for a single-user finance tool.

Engine creation is lazy: it only happens when the first DB session
is requested, not at import time. This lets the API module load
even when DATABASE_URL is absent (e.g. for tests or the export-only
Docker image).
"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session as Session  # noqa: F401 – re-exported
from sqlalchemy.orm import sessionmaker

from src.core.config import settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine())


def get_db():
    """FastAPI dependency that yields a DB session per request."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
