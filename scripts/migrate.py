"""Run Alembic migrations against the configured DATABASE_URL.

Invoked as an init container / pre-start hook in the Helm chart, and
usable locally via `uv run python scripts/migrate.py upgrade`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"


def _config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def main(argv: list[str]) -> int:
    target = argv[1] if len(argv) > 1 else "upgrade"
    cfg = _config()
    if target == "upgrade":
        command.upgrade(cfg, "heads")
    elif target == "downgrade":
        command.downgrade(cfg, "base")
    elif target == "current":
        command.current(cfg)
    else:
        print(f"unknown target: {target}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
