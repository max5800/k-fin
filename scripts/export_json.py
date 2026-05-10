#!/usr/bin/env python
"""CLI — Comdirect JSON export using Pydantic models as source of truth.

Produces a clean, typed JSON file suitable for downstream agent and report
consumers.  The schema mirrors the Pydantic models in
``src.external.models`` — no ad-hoc field mapping.

Usage:
    uv run python scripts/export_json.py [--output-dir DIR] [--since 30d] [--pretty]
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from src.external.comdirect_client import ComdirectClient
from src.core.config import settings  # noqa: F401 — ensures .env is loaded
from src.core.logging import get_logger, setup_logging
from src.exporter.json_export import build_export

setup_logging()
logger = get_logger("export_json_cli")


def parse_since(since_str: str | None) -> date | None:
    """Parse ``--since``: accepts YYYY-MM-DD or Xd (e.g. 30d)."""
    if not since_str:
        return None
    m = re.fullmatch(r"(\d+)d", since_str)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(since_str)
    except ValueError:
        raise ValueError(
            f"Invalid --since format: '{since_str}'. Expected: YYYY-MM-DD or Xd (e.g. 30d)"
        )


async def _run(output_dir: str, since: date | None, pretty: bool) -> None:
    client = ComdirectClient()

    logger.info("Starting Comdirect auth flow…")
    try:
        ok = await client.authenticate_full()
    except Exception as exc:
        logger.error(f"Auth failed: {exc}")
        print(f"\nERROR: Authentication failed — {exc}", file=sys.stderr)
        sys.exit(1)

    if not ok:
        logger.error("Authentication failed. Check credentials or TAN.")
        sys.exit(1)

    logger.info("Fetching all data…")
    raw = await client.get_all_data(
        account_transaction_limit=settings.account_transaction_limit,
        account_transaction_min_booking_date=settings.account_transaction_min_booking_date,
        depot_transaction_limit=settings.depot_transaction_limit,
        depot_transaction_min_booking_date=settings.depot_transaction_min_booking_date,
    )

    logger.info("Building JSON export…")
    payload = build_export(raw, since=since)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    filename = f"comdirect_export_{date.today().isoformat()}.json"
    filepath = out_path / filename

    indent = 2 if pretty else None
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=indent)

    meta = payload["meta"]
    logger.info(f"Exported to {filepath}")
    print(f"\n{'=' * 50}")
    print(f"Accounts     : {meta['account_count']}")
    print(f"Transactions : {meta['transaction_count']}")
    print(f"Positions    : {meta['position_count']}")
    print(f"Depot txns   : {meta['depot_transaction_count']}")
    if meta["since"]:
        print(f"Since        : {meta['since']}")
    print(f"{'=' * 50}")
    print(f"Export path  : {filepath.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Comdirect financial data to JSON (model-based)."
    )
    parser.add_argument(
        "--output-dir",
        default="exports",
        metavar="DIR",
        help="Output directory (default: exports/)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only include transactions from this date. Format: YYYY-MM-DD or Xd (e.g. 30d)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON with indent=2",
    )
    args = parser.parse_args()

    since = parse_since(args.since)
    if since:
        logger.info(f"Date filter: since {since.isoformat()}")

    asyncio.run(_run(args.output_dir, since, args.pretty))


if __name__ == "__main__":
    main()
