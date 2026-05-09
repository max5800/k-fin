#!/usr/bin/env python
"""Read-only refund backfill report.

Migration 0016 introduced `is_refund` on `normalized_transactions`. All
existing rows were defaulted to `False` — but historic transactions in the
`erstattungen` category are split between *real* income (Steuerrückzahlung,
Cashback) and *refunds* that should be folded into their original expense
category (Krankenkasse, Splitwise, Spesen).

This script does NOT mutate data. It lists every positive-amount transaction
currently in `erstattungen` so the user can decide which should be re-routed
through the categorization agent (`POST /api/v1/runs/{id}/rerun`) or
manually flipped via `PATCH /transactions/{id}`.

Usage:
    uv run python scripts/refund_backfill_report.py
    uv run python scripts/refund_backfill_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db.categories import INCOME_CATCHALL_CATEGORY_ID
from src.core.db.models import NormalizedTransaction


def _format_human(rows: list[dict]) -> str:
    if not rows:
        return "Keine Transaktionen in Kategorie 'erstattungen' gefunden.\n"

    lines = [
        f"{len(rows)} Transaktion(en) in Kategorie 'erstattungen' gefunden.",
        "",
        "Manuell prüfen — typische Kandidaten für is_refund=True:",
        "  - Krankenkasse, private Krankenversicherung",
        "  - Splitwise, PayPal-Friends, Privatperson",
        "  - Arbeitgeber-Spesen, Reisekosten",
        "  - Amazon / Onlineshop-Retoure",
        "",
        "Echtes Einkommen (is_refund=False bleibt) — typisch:",
        "  - Finanzamt (Steuerrückzahlung)",
        "  - Cashback / Bonusprogramme",
        "  - Zinsgutschriften",
        "",
        "─" * 80,
    ]
    for r in rows:
        sender = (r["sender"] or r["recipient"] or "?").strip()
        lines.append(
            f"{r['booking_date']} | {r['amount']:>10.2f} EUR | "
            f"{sender[:35]:<35} | {(r['description'] or '')[:40]}"
        )
        lines.append(f"  id={r['id']}")
    lines.append("─" * 80)
    lines.append(
        "\nNächste Schritte:"
        "\n  - Refund: PATCH /api/v1/transactions/<id> {\"is_refund\": true, "
        "\"category_id\": \"<original-kategorie>\"}"
        "\n  - Echtes Einkommen: nichts tun, bleibt in 'erstattungen' mit is_refund=False"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    parser.add_argument(
        "--category-id",
        default=INCOME_CATCHALL_CATEGORY_ID,
        help=f"Category to inspect (default: {INCOME_CATCHALL_CATEGORY_ID})",
    )
    args = parser.parse_args()

    if not settings.database_url:
        print("DATABASE_URL not configured.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url)
    with Session(engine) as s:
        stmt = (
            select(
                NormalizedTransaction.id,
                NormalizedTransaction.booking_date,
                NormalizedTransaction.amount,
                NormalizedTransaction.sender,
                NormalizedTransaction.recipient,
                NormalizedTransaction.description,
                NormalizedTransaction.is_refund,
            )
            .where(NormalizedTransaction.category_id == args.category_id)
            .where(NormalizedTransaction.amount > 0)
            .order_by(NormalizedTransaction.booking_date.desc())
        )
        rows = [
            {
                "id": r.id,
                "booking_date": r.booking_date.isoformat()
                if isinstance(r.booking_date, date)
                else r.booking_date,
                "amount": float(r.amount),
                "sender": r.sender,
                "recipient": r.recipient,
                "description": r.description,
                "is_refund": r.is_refund,
            }
            for r in s.execute(stmt).all()
        ]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(_format_human(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
