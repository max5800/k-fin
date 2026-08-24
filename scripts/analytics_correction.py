"""Dry-run/apply entrypoint for trustworthy analytics correction."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.config import settings
from src.services.analytics_correction import run_correction


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or apply the non-destructive analytics correction"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply reversible status/version/link updates (default: dry-run)",
    )
    args = parser.parse_args()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            result = run_correction(session, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
