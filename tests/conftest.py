"""Shared test fixtures.

Integration tests use a throw-away PostgreSQL instance via
testcontainers. If Docker is not available locally, those tests are
skipped — the unit-test suite still runs offline.
"""

from __future__ import annotations

import shutil

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.core.db.models import Base


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    import subprocess

    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5, text=True)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def postgres_url():
    if not _docker_available():
        pytest.skip("Docker not available — integration tests need testcontainers")

    import time

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        # Wait until Postgres actually accepts connections — testcontainers
        # sometimes yields before the socket is fully ready.
        engine = create_engine(url)
        for _ in range(30):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                time.sleep(0.3)
        engine.dispose()
        yield url


@pytest.fixture
def db_engine(postgres_url):
    engine = create_engine(postgres_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(text(f'DELETE FROM "{table.name}"'))
        engine.dispose()


@pytest.fixture
def db_session(db_engine):
    with Session(db_engine) as session:
        yield session
