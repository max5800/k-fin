"""Shared test fixtures.

Integration tests use a throw-away PostgreSQL instance via
testcontainers. If Docker is not available locally, those tests are
skipped — the unit-test suite still runs offline.
"""

from __future__ import annotations

import os
import shutil

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.core.db.models import Base

# Ensure agent modules can be imported under test even without a real
# Anthropic key set in the environment. Pydantic-AI instantiates the
# Anthropic provider at module load time and would otherwise raise.
# Tests that exercise actual LLM calls override the model with TestModel.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key-not-used")

# Rancher Desktop compatibility for testcontainers: the desktop socket
# at ~/.rd/docker.sock isn't reachable from inside the Docker VM, so
# Ryuk's bind-mount of that path fails. Tell testcontainers to use the
# in-VM /var/run/docker.sock (visible to containers) and disable Ryuk
# — without these, every integration test errors with `mkdir
# /Users/.../docker.sock: operation not supported`.
if os.path.exists(os.path.expanduser("~/.rd/docker.sock")):
    os.environ.setdefault("DOCKER_HOST", "unix://" + os.path.expanduser("~/.rd/docker.sock"))
    os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


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
    external_url = os.environ.get("KFIN_TEST_POSTGRES_URL")
    if external_url:
        yield external_url
        return

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
