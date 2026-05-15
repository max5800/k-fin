"""Forward + reverse migration tests.

The CI `test-migrations` job only runs `upgrade head` once. These tests
add real coverage: full upgrade/downgrade reversibility, and a targeted
data-integrity check for `0022_external_source` (the M16-P1 schema
generalization) — the backfill of `external_id` from `comdirect_id` and
its restoration on downgrade.

Each test runs against its own throw-away PostgreSQL container so the
schema mutations never leak into the shared integration-test database.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

_ROOT = Path(__file__).resolve().parent.parent


def _docker_available() -> bool:
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


@contextlib.contextmanager
def _force_db_url(url: str):
    """Point *every* `Settings` instance at `url` for the duration.

    alembic's `env.py` resolves the target DB from `settings.database_url`.
    The codebase carries more than one `Settings` instance, and other
    integration tests mutate them globally without restoring — so a
    single `monkeypatch.setattr` is not enough to guarantee alembic hits
    *this* test's container. Patch them all, restore them all.
    """
    from src.core.config import Settings

    patched: list[tuple[object, str]] = []
    seen: set[int] = set()
    for module in list(sys.modules.values()):
        candidate = getattr(module, "settings", None)
        if isinstance(candidate, Settings) and id(candidate) not in seen:
            seen.add(id(candidate))
            patched.append((candidate, candidate.database_url))
            candidate.database_url = url
    try:
        yield
    finally:
        for instance, original in patched:
            instance.database_url = original


@pytest.fixture
def fresh_db_url():
    """A brand-new empty PostgreSQL database — isolated per test."""
    if not _docker_available():
        pytest.skip("Docker not available — migration tests need testcontainers")

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
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


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return cfg


def test_full_upgrade_downgrade_roundtrip(fresh_db_url):
    """upgrade head → downgrade base → upgrade head must all succeed."""
    from alembic import command

    cfg = _alembic_config()
    with _force_db_url(fresh_db_url):
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

    engine = create_engine(fresh_db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"raw_transactions", "normalized_transactions", "sync_runs"} <= tables
    finally:
        engine.dispose()


def test_0022_backfills_external_id_and_is_reversible(fresh_db_url):
    """0022 backfills external_id from comdirect_id; downgrade restores it."""
    from alembic import command

    cfg = _alembic_config()
    engine = create_engine(fresh_db_url)

    try:
        with _force_db_url(fresh_db_url):
            # Pre-0022 state: raw_transactions still has `comdirect_id`.
            command.upgrade(cfg, "0021_drop_reports_file_path")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO raw_transactions "
                        "(content_hash, comdirect_id, raw_data, version) "
                        "VALUES (:h, :cid, :raw, 1)"
                    ),
                    {"h": "hash-cd-1", "cid": "CD-123", "raw": "{}"},
                )
            cols_before = {
                c["name"] for c in inspect(engine).get_columns("raw_transactions")
            }
            assert "comdirect_id" in cols_before and "external_id" not in cols_before

            # Forward: 0022.
            command.upgrade(cfg, "0022_external_source")
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT external_id, source FROM raw_transactions "
                        "WHERE content_hash = 'hash-cd-1'"
                    )
                ).one()
            assert row.external_id == "CD-123"
            assert row.source == "comdirect"

            insp = inspect(engine)
            cols_after = {c["name"] for c in insp.get_columns("raw_transactions")}
            assert "external_id" in cols_after and "comdirect_id" not in cols_after
            index_names = {ix["name"] for ix in insp.get_indexes("raw_transactions")}
            assert "ix_raw_transactions_source_external_id" in index_names

            # Reverse: back to 0021 restores comdirect_id from external_id.
            command.downgrade(cfg, "0021_drop_reports_file_path")
            with engine.connect() as conn:
                restored = conn.execute(
                    text(
                        "SELECT comdirect_id FROM raw_transactions "
                        "WHERE content_hash = 'hash-cd-1'"
                    )
                ).scalar_one()
            assert restored == "CD-123"
            cols_reverted = {
                c["name"] for c in inspect(engine).get_columns("raw_transactions")
            }
            assert "comdirect_id" in cols_reverted and "external_id" not in cols_reverted
    finally:
        engine.dispose()


def test_0023_renames_sync_enum_type(fresh_db_url):
    """0023 renames the Postgres enum type sync_source_enum → sync_stage_enum."""
    from alembic import command

    cfg = _alembic_config()
    engine = create_engine(fresh_db_url)

    enum_query = text("SELECT 1 FROM pg_type WHERE typname = :name")
    try:
        with _force_db_url(fresh_db_url):
            command.upgrade(cfg, "0022_external_source")
            with engine.connect() as conn:
                assert conn.execute(enum_query, {"name": "sync_source_enum"}).first()

            command.upgrade(cfg, "0023_rename_sync_source_to_stage")
            with engine.connect() as conn:
                assert conn.execute(enum_query, {"name": "sync_stage_enum"}).first()
                assert not conn.execute(
                    enum_query, {"name": "sync_source_enum"}
                ).first()

            command.downgrade(cfg, "0022_external_source")
            with engine.connect() as conn:
                assert conn.execute(enum_query, {"name": "sync_source_enum"}).first()
    finally:
        engine.dispose()
