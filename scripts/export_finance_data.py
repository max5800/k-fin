"""CLI — export all Comdirect data to a Finance Agent JSON file.

Usage:
    uv run python scripts/export_finance_data.py [--output-dir DIR] [--pretty]
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from src.connector.comdirect_client import ComdirectClient
from src.core.config import settings  # noqa: F401 — ensures .env is loaded
from src.core.logging import get_logger, setup_logging
from src.exporter.finance_agent_mapper import map_to_finance_agent

setup_logging()
logger = get_logger("export_cli")


def _build_summary(data: dict) -> str:
    """Return a human-readable summary string from Finance Agent output."""
    lines: list[str] = []

    meta = data.get("meta", {})
    lines.append(f"Exported at : {meta.get('exported_at', 'n/a')}")
    lines.append(f"Accounts    : {meta.get('account_count', 0)}")
    lines.append(f"Total txns  : {meta.get('total_transaction_count', 0)}")
    lines.append("")

    # Per-account transaction counts
    non_meta_keys = [k for k in data if k not in ("meta", "depot")]
    if non_meta_keys:
        lines.append("Transactions per account:")
        for key in non_meta_keys:
            entry = data[key]
            count = entry.get("summary", {}).get("count", len(entry.get("transactions", [])))
            lines.append(f"  {key:<28} {count:>5} transactions")

    # Depot total value
    depot = data.get("depot", {})
    depot_summary = depot.get("summary", {})
    if depot.get("depot_id"):
        total_value = depot_summary.get("total_value", 0.0)
        currency = "EUR"
        # Try to extract currency from first position
        for pos in depot.get("positions", []):
            if pos.get("currency"):
                currency = pos["currency"]
                break
        gains = depot_summary.get("total_gains", 0.0)
        gains_pct = depot_summary.get("total_gains_percent", 0.0)
        pos_count = depot_summary.get("position_count", 0)
        lines.append("")
        lines.append(f"Depot total value : {total_value:,.2f} {currency}")
        lines.append(f"  Gains           : {gains:+,.2f} {currency} ({gains_pct:+.2f}%)")
        lines.append(f"  Positions       : {pos_count}")
        lines.append(f"  Depot txns      : {len(depot.get('transactions', []))}")

    return "\n".join(lines)


async def _run(output_dir: str, pretty: bool) -> None:
    client = ComdirectClient()

    logger.info("Starting Comdirect full auth flow…")
    try:
        ok = await client.authenticate_full()
    except Exception as exc:
        logger.error(f"Auth exception: {exc}")
        print(f"\nERROR: Authentication failed — {exc}", file=sys.stderr)
        sys.exit(1)

    if not ok:
        msg = "Authentication failed. Check credentials / TAN confirmation."
        logger.error(msg)
        print(f"\nERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    logger.info("Auth complete. Fetching all data…")
    try:
        raw = await client.get_all_data(
            account_transaction_limit=settings.account_transaction_limit,
            account_transaction_min_booking_date=settings.account_transaction_min_booking_date,
            depot_transaction_limit=settings.depot_transaction_limit,
            depot_transaction_min_booking_date=settings.depot_transaction_min_booking_date,
        )
    except Exception as exc:
        logger.error(f"Data fetch failed: {exc}")
        print(f"\nERROR: Failed to fetch data — {exc}", file=sys.stderr)
        sys.exit(1)

    logger.info("Mapping to Finance Agent format…")
    mapped = map_to_finance_agent(raw)

    # Write output file
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"finance_export_{date.today().isoformat()}.json"
    out_path = out_dir / filename

    indent = 2 if pretty else None
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(mapped, fh, ensure_ascii=False, indent=indent)

    logger.info(f"Exported to {out_path}")

    # Print summary
    print("\n" + "=" * 50)
    print(_build_summary(mapped))
    print("=" * 50)
    print(f"\nExport path: {out_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Comdirect financial data to Finance Agent JSON format."
    )
    parser.add_argument(
        "--output-dir",
        default="data/",
        metavar="DIR",
        help="Directory for the output file (default: data/)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON with indent=2",
    )
    args = parser.parse_args()

    asyncio.run(_run(args.output_dir, args.pretty))


if __name__ == "__main__":
    main()
