"""Schema-snapshot drift test.

Migrates a throw-away database to `head`, introspects the resulting
schema (tables, columns, indexes, foreign keys, enum types) into a
deterministic structure, and diffs it against a committed golden
snapshot. Any migration that changes the schema without a corresponding,
reviewed update to `tests/fixtures/schema_snapshot.json` fails CI.

Regenerate the golden file deliberately after an intended schema change:

    REGEN_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_snapshot.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

_ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def _force_db_url(url: str):
    """Point every `Settings` instance at `url` while alembic runs.

    See tests/test_migrations.py for why a single monkeypatch is not
    enough — the codebase carries multiple `Settings` instances.
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
_GOLDEN = _ROOT / "tests" / "fixtures" / "schema_snapshot.json"


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


def _build_snapshot(engine) -> dict:
    """Deterministic structural snapshot of the migrated schema."""
    insp = inspect(engine)
    tables: dict[str, dict] = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue
        columns = {
            c["name"]: {
                "type": str(c["type"]),
                "nullable": bool(c["nullable"]),
            }
            for c in insp.get_columns(table)
        }
        indexes = {
            ix["name"]: {
                "columns": list(ix["column_names"]),
                "unique": bool(ix["unique"]),
            }
            for ix in insp.get_indexes(table)
        }
        fks = sorted(
            (
                f"{fk['constrained_columns']}->"
                f"{fk['referred_table']}.{fk['referred_columns']}"
            )
            for fk in insp.get_foreign_keys(table)
        )
        tables[table] = {
            "columns": dict(sorted(columns.items())),
            "primary_key": sorted(insp.get_pk_constraint(table)["constrained_columns"]),
            "indexes": dict(sorted(indexes.items())),
            "foreign_keys": fks,
        }

    with engine.connect() as conn:
        enum_rows = conn.execute(
            text(
                "SELECT t.typname, e.enumlabel "
                "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                "ORDER BY t.typname, e.enumsortorder"
            )
        ).all()
    enums: dict[str, list[str]] = {}
    for typname, label in enum_rows:
        enums.setdefault(typname, []).append(label)

    return {"tables": tables, "enums": enums}


def test_schema_matches_golden_snapshot():
    if not _docker_available():
        pytest.skip("Docker not available — schema snapshot needs testcontainers")

    from alembic import command
    from alembic.config import Config
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

        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))
        with _force_db_url(url):
            command.upgrade(cfg, "head")

        try:
            snapshot = _build_snapshot(engine)
        finally:
            engine.dispose()

    if os.environ.get("REGEN_SCHEMA_SNAPSHOT") == "1":
        _GOLDEN.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        pytest.skip("regenerated schema_snapshot.json")

    assert _GOLDEN.exists(), (
        "schema_snapshot.json missing — generate it with "
        "REGEN_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_snapshot.py"
    )
    golden = json.loads(_GOLDEN.read_text())
    assert snapshot == golden, (
        "Schema drift vs. golden snapshot. If intentional, regenerate with "
        "REGEN_SCHEMA_SNAPSHOT=1 uv run pytest tests/test_schema_snapshot.py"
    )
