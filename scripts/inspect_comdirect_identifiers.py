#!/usr/bin/env python
"""Read-only identifier audit for Comdirect raw transactions.

The Comdirect REST API leaves `transactionId` empty for *booked*
transactions, so `raw_transactions.external_id` is NULL for every
Comdirect row — which disables version-chain (`superseded_by`)
detection. This script does NOT mutate data. It inspects the captured
`raw_data` payloads and reports, per candidate field, how reliably it
could serve as a stable logical identifier.

For each scalar field found in the payload it reports:
  - fill rate   — share of rows where the field is non-empty
  - uniqueness  — distinct non-empty values / non-empty rows
A good `external_id` source is ~100 % fill **and** ~100 % unique.

Only rows synced after the `source_payload` capture (plan M16-P1-B1)
carry the full upstream API dict; older rows hold just the flattened
11-field export. The script handles both and flags how many rows are
"full-payload" so the audit isn't read off stale partial data.

Usage:
    uv run python scripts/inspect_comdirect_identifiers.py
    uv run python scripts/inspect_comdirect_identifiers.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db.models import DataSource, RawTransaction

# Fields worth calling out explicitly in the human report — the known
# Comdirect identifier candidates per the REST API swagger.
KNOWN_CANDIDATES = (
    "transactionId",
    "reference",
    "endToEndReference",
    "directDebitMandateId",
    "directDebitCreditorId",
)


def _payload(raw_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (upstream-payload, is_full_capture).

    Post-B1 rows nest the unmodified API dict under `source_payload`;
    older rows only have the flattened export — returned as-is.
    """
    sp = raw_data.get("source_payload")
    if isinstance(sp, dict) and sp:
        return sp, True
    return raw_data, False


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == {} or value == []


def audit(engine) -> dict[str, Any]:
    with Session(engine) as session:
        rows = (
            session.execute(
                select(RawTransaction.raw_data).where(
                    RawTransaction.source == DataSource.COMDIRECT
                )
            )
            .scalars()
            .all()
        )

    total = len(rows)
    full_capture = 0
    non_empty: dict[str, int] = defaultdict(int)
    values: dict[str, list[Any]] = defaultdict(list)

    for raw_data in rows:
        payload, is_full = _payload(raw_data or {})
        if is_full:
            full_capture += 1
        for key, value in payload.items():
            # Only scalar fields can act as an identifier.
            if isinstance(value, (dict, list)):
                continue
            if not _is_empty(value):
                non_empty[key] += 1
                values[key].append(value)

    fields: dict[str, dict[str, Any]] = {}
    for key in sorted(set(non_empty) | set(KNOWN_CANDIDATES)):
        filled = non_empty.get(key, 0)
        distinct = len(set(values.get(key, [])))
        fields[key] = {
            "fill_rate": round(filled / total, 4) if total else 0.0,
            "filled": filled,
            "distinct": distinct,
            "uniqueness": round(distinct / filled, 4) if filled else 0.0,
            "known_candidate": key in KNOWN_CANDIDATES,
        }

    return {
        "total_comdirect_rows": total,
        "full_payload_rows": full_capture,
        "fields": fields,
    }


def _format_human(report: dict[str, Any]) -> str:
    total = report["total_comdirect_rows"]
    full = report["full_payload_rows"]
    lines = [
        f"Comdirect raw_transactions: {total} rows "
        f"({full} with full source_payload, "
        f"{total - full} legacy flat-export only)",
        "",
    ]
    if total == 0:
        return lines[0] + "\nNo Comdirect rows — run a live sync first.\n"
    if full == 0:
        lines.append(
            "WARNING: no row carries a full payload yet. Run one live sync "
            "after the B1 capture change before trusting this audit.\n"
        )

    header = f"{'field':<26} {'fill':>8} {'distinct':>10} {'unique':>8}  candidate"
    lines += [header, "-" * len(header)]
    # Known candidates first, then the rest.
    ordered = sorted(
        report["fields"].items(),
        key=lambda kv: (not kv[1]["known_candidate"], kv[0]),
    )
    for name, s in ordered:
        mark = "  <-- candidate" if s["known_candidate"] else ""
        lines.append(
            f"{name:<26} {s['fill_rate'] * 100:>7.1f}% "
            f"{s['distinct']:>10} {s['uniqueness'] * 100:>7.1f}%{mark}"
        )
    lines += [
        "",
        "A usable external_id source needs ~100% fill AND ~100% unique.",
        "If none qualifies: accept NULL external_id for Comdirect "
        "(version chains stay disabled, documented) or derive a "
        "synthetic key — see plan M16-P1-Follow-up.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human-readable text"
    )
    args = parser.parse_args()

    if not settings.database_url:
        print("DATABASE_URL not configured.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url)
    report = audit(engine)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_format_human(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
