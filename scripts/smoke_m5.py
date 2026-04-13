"""M5 smoke checks.

Read-only verification that the normalization layer is wired up:
  - DB reachable
  - Alembic at HEAD
  - Core tables present
  - Row counts reported

Usage:
    uv run python scripts/smoke_m5.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from src.core.config import settings

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_TABLES = {
    "raw_transactions",
    "normalized_transactions",
    "categories",
    "rules",
    "recurring_patterns",
    "sync_runs",
    "alembic_version",
}


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    return 1


def main() -> int:
    if not settings.database_url:
        return _fail("DATABASE_URL not set")

    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print(f"OK: connected to {engine.url.render_as_string(hide_password=True)}")

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current != head:
        return _fail(f"alembic not at head: current={current} head={head}")
    print(f"OK: alembic at head ({head})")

    insp = inspect(engine)
    present = set(insp.get_table_names())
    missing = REQUIRED_TABLES - present
    if missing:
        return _fail(f"missing tables: {sorted(missing)}")
    print(f"OK: all required tables present ({len(REQUIRED_TABLES)})")

    with engine.connect() as conn:
        for tbl in sorted(REQUIRED_TABLES - {"alembic_version"}):
            count = conn.execute(text(f"SELECT count(*) FROM {tbl}")).scalar()
            print(f"  {tbl}: {count} rows")

    return 0


if __name__ == "__main__":
    sys.exit(main())
