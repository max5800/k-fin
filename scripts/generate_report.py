#!/usr/bin/env python
"""
Generate a compact Markdown financial report from Finance Agent JSON.

Reads a JSON file produced by the finance agent mapper (or uses built-in
demo data for development) and writes a human-readable Markdown summary.

Usage:
    uv run python scripts/generate_report.py --input data.json --output report.md
    uv run python scripts/generate_report.py --demo              # uses sample data
"""

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path


def _fmt_eur(amount: float) -> str:
    """Format a float as EUR string: 1234.5 -> '1.234,50 EUR'."""
    sign = "-" if amount < 0 else ""
    abs_val = abs(amount)
    integer_part = int(abs_val)
    decimal_part = round((abs_val - integer_part) * 100)
    # German thousands separator
    int_str = f"{integer_part:,}".replace(",", ".")
    return f"{sign}{int_str},{decimal_part:02d} EUR"


def _fmt_pct(pct: float) -> str:
    """Format percentage: 13.636 -> '+13,64 %'."""
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f} %".replace(".", ",")


def _is_account_entry(value: object) -> bool:
    """Check if a value looks like a Finance Agent account entry."""
    return isinstance(value, dict) and ("transactions" in value or "summary" in value)


def _normalize_input(data: dict) -> dict:
    """Detect input format and normalise to Finance Agent JSON.

    Accepts:
      - Finance Agent format (account keys + depot + meta) — returned as-is.
      - Raw Comdirect get_all_data() format (accounts list + transactions dict)
        — converted via the mapper if available.

    Raises ValueError for unrecognisable shapes.
    """
    # Already Finance Agent format: has at least one account-shaped entry or a meta key
    has_account_entries = any(
        _is_account_entry(v) for k, v in data.items() if k not in ("depot", "meta")
    )
    if has_account_entries or ("meta" in data and "depot" in data):
        return data

    # Raw Comdirect format: has "accounts" list
    if "accounts" in data and isinstance(data["accounts"], list):
        try:
            from src.exporter.finance_agent_mapper import map_to_finance_agent
        except ImportError:
            raise ValueError(
                "Input looks like raw Comdirect format but the finance_agent_mapper "
                "module is not available for conversion."
            )
        return map_to_finance_agent(data)

    raise ValueError(
        "Unrecognised input format. Expected Finance Agent JSON "
        "(keys like 'girokonto', 'depot', 'meta') or raw Comdirect export "
        "(key 'accounts' with a list)."
    )


def generate_report(data: dict, report_date: str | None = None) -> str:
    """Generate markdown report from Finance Agent format data.

    Also accepts raw Comdirect get_all_data() dicts — these are auto-converted
    via the finance_agent_mapper when available.

    Args:
        data: Finance Agent format dict, or raw Comdirect export dict.
        report_date: Optional override for report date (YYYY-MM-DD).

    Returns:
        Markdown string.
    """
    data = _normalize_input(data)
    report_date = report_date or date.today().isoformat()
    meta = data.get("meta", {})
    lines: list[str] = []

    lines.append(f"# Finanzreport — {report_date}")
    lines.append("")
    exported_at = meta.get("exported_at", "unbekannt")
    lines.append(f"*Datenstand: {exported_at}*")
    lines.append("")

    # ── 1. Balance overview ────────────────────────────────────────────
    lines.append("## Kontenübersicht")
    lines.append("")
    lines.append("| Konto | Ein | Aus | Netto | Transaktionen |")
    lines.append("|-------|-----|-----|-------|---------------|")

    total_in = 0.0
    total_out = 0.0
    all_transactions: list[dict] = []
    account_keys = [k for k in data if k not in ("depot", "meta") and _is_account_entry(data[k])]

    for key in sorted(account_keys):
        entry = data[key]
        txs = entry.get("transactions", [])
        summary = entry.get("summary")
        # Recompute summary from transactions when missing or incomplete
        if not summary or not isinstance(summary, dict) or "total_in" not in summary:
            s_in = sum(t.get("amount", 0) for t in txs if t.get("amount", 0) > 0)
            s_out = abs(sum(t.get("amount", 0) for t in txs if t.get("amount", 0) < 0))
            summary = {
                "total_in": round(s_in, 2),
                "total_out": round(s_out, 2),
                "net": round(s_in - s_out, 2),
                "count": len(txs),
            }
        t_in = summary.get("total_in", 0.0)
        t_out = summary.get("total_out", 0.0)
        net = summary.get("net", 0.0)
        count = summary.get("count", 0)
        total_in += t_in
        total_out += t_out
        all_transactions.extend(txs)
        label = key.replace("_", " ").title()
        lines.append(
            f"| {label} | {_fmt_eur(t_in)} | {_fmt_eur(t_out)} | {_fmt_eur(net)} | {count} |"
        )

    lines.append(
        f"| **Gesamt** | **{_fmt_eur(total_in)}** | **{_fmt_eur(total_out)}** "
        f"| **{_fmt_eur(total_in - total_out)}** | **{len(all_transactions)}** |"
    )
    lines.append("")

    # ── 2. Top spending counterparts ───────────────────────────────────
    if all_transactions:
        spending: Counter[str] = Counter()
        for tx in all_transactions:
            if tx.get("amount", 0) < 0:
                name = tx.get("counterpart_name") or "Unbekannt"
                spending[name] += abs(tx["amount"])

        if spending:
            lines.append("## Top-Ausgaben nach Empfänger")
            lines.append("")
            lines.append("| Empfänger | Summe | Anzahl |")
            lines.append("|-----------|-------|--------|")

            # Count occurrences for the frequency column
            freq: Counter[str] = Counter()
            for tx in all_transactions:
                if tx.get("amount", 0) < 0:
                    name = tx.get("counterpart_name") or "Unbekannt"
                    freq[name] += 1

            for name, total in spending.most_common(10):
                lines.append(f"| {name} | {_fmt_eur(total)} | {freq[name]}x |")
            lines.append("")

    # ── 3. Largest single transactions ─────────────────────────────────
    if all_transactions:
        sorted_txs = sorted(all_transactions, key=lambda t: abs(t.get("amount", 0)), reverse=True)
        top_n = sorted_txs[:10]

        lines.append("## Größte Einzelbuchungen")
        lines.append("")
        lines.append("| Datum | Betrag | Gegenpartei | Beschreibung |")
        lines.append("|-------|--------|-------------|--------------|")

        for tx in top_n:
            tx_date = tx.get("date", "?")
            amount = tx.get("amount", 0)
            name = tx.get("counterpart_name") or "—"
            desc = tx.get("text") or tx.get("type") or "—"
            # Truncate long descriptions
            if len(desc) > 50:
                desc = desc[:47] + "..."
            lines.append(f"| {tx_date} | {_fmt_eur(amount)} | {name} | {desc} |")
        lines.append("")

    # ── 4. Recurring counterparts (3+ transactions) ────────────────────
    if all_transactions:
        counterpart_count: Counter[str] = Counter()
        for tx in all_transactions:
            name = tx.get("counterpart_name")
            if name:
                counterpart_count[name] += 1

        recurring = [(n, c) for n, c in counterpart_count.most_common() if c >= 3]
        if recurring:
            lines.append("## Wiederkehrende Gegenparteien (≥ 3 Buchungen)")
            lines.append("")
            lines.append("| Gegenpartei | Buchungen |")
            lines.append("|-------------|-----------|")
            for name, count in recurring[:10]:
                lines.append(f"| {name} | {count}x |")
            lines.append("")

    # ── 5. Depot overview ──────────────────────────────────────────────
    depot = data.get("depot") or {}
    positions = depot.get("positions") or []
    depot_summary = depot.get("summary")
    # Recompute depot summary from positions when missing
    if positions and (not depot_summary or "total_value" not in depot_summary):
        tv = sum(p.get("current_value", 0) for p in positions)
        tp = sum(p.get("purchase_value", 0) for p in positions)
        tg = round(tv - tp, 2)
        depot_summary = {
            "total_value": round(tv, 2),
            "total_purchase_value": round(tp, 2),
            "total_gains": tg,
            "total_gains_percent": round((tg / tp * 100) if tp else 0, 4),
            "position_count": len(positions),
        }
    depot_summary = depot_summary or {}

    if positions:
        lines.append("## Depot")
        lines.append("")
        lines.append("| Wertpapier | ISIN | Stück | Wert | Gewinn/Verlust |")
        lines.append("|------------|------|-------|------|----------------|")

        for pos in sorted(positions, key=lambda p: p.get("current_value", 0), reverse=True):
            name = pos.get("name", "?")
            isin = pos.get("isin", "?")
            qty = pos.get("quantity", 0)
            value = pos.get("current_value", 0)
            gains = pos.get("gains", 0)
            gains_pct = pos.get("gains_percent", 0)
            lines.append(
                f"| {name} | {isin} | {qty:g} | {_fmt_eur(value)} "
                f"| {_fmt_eur(gains)} ({_fmt_pct(gains_pct)}) |"
            )

        tv = depot_summary.get("total_value", 0)
        tg = depot_summary.get("total_gains", 0)
        tgp = depot_summary.get("total_gains_percent", 0)
        lines.append(
            f"| **Gesamt** | | | **{_fmt_eur(tv)}** | **{_fmt_eur(tg)} ({_fmt_pct(tgp)})** |"
        )
        lines.append("")

    # ── 6. Depot transactions ──────────────────────────────────────────
    depot_txs = depot.get("transactions", [])
    if depot_txs:
        lines.append("## Depot-Transaktionen")
        lines.append("")
        lines.append("| Datum | Art | Wertpapier | Stück | Betrag |")
        lines.append("|-------|-----|------------|-------|--------|")

        for tx in sorted(depot_txs, key=lambda t: t.get("date", ""), reverse=True)[:10]:
            lines.append(
                f"| {tx.get('date', '?')} | {tx.get('transaction_type', '?')} "
                f"| {tx.get('name', '?')} | {tx.get('quantity', 0):g} "
                f"| {_fmt_eur(tx.get('amount', 0))} |"
            )
        lines.append("")

    # ── Footer ─────────────────────────────────────────────────────────
    lines.append("---")
    lines.append(f"*Generiert am {report_date} · k-fin*")
    lines.append("")

    return "\n".join(lines)


def _build_demo_data() -> dict:
    """Return realistic demo data in Finance Agent format."""
    return {
        "girokonto": {
            "account_id": "ACC001",
            "transactions": [
                {
                    "date": "2026-04-01",
                    "booking_date": "2026-04-01",
                    "value_date": "2026-04-01",
                    "type": "Gehalt",
                    "text": "Gehalt April",
                    "amount": 3200.00,
                    "currency": "EUR",
                    "counterpart_name": "Arbeitgeber GmbH",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX001",
                },
                {
                    "date": "2026-04-02",
                    "booking_date": "2026-04-02",
                    "value_date": "2026-04-02",
                    "type": "Lastschrift",
                    "text": "Miete April",
                    "amount": -850.00,
                    "currency": "EUR",
                    "counterpart_name": "Hausverwaltung Müller",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX002",
                },
                {
                    "date": "2026-04-03",
                    "booking_date": "2026-04-03",
                    "value_date": "2026-04-03",
                    "type": "Lastschrift",
                    "text": "Einkauf Lebensmittel",
                    "amount": -67.89,
                    "currency": "EUR",
                    "counterpart_name": "REWE GmbH",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX003",
                },
                {
                    "date": "2026-04-04",
                    "booking_date": "2026-04-04",
                    "value_date": "2026-04-04",
                    "type": "Lastschrift",
                    "text": "Einkauf",
                    "amount": -23.45,
                    "currency": "EUR",
                    "counterpart_name": "REWE GmbH",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX004",
                },
                {
                    "date": "2026-04-05",
                    "booking_date": "2026-04-05",
                    "value_date": "2026-04-05",
                    "type": "Lastschrift",
                    "text": "Einkauf Bio",
                    "amount": -34.12,
                    "currency": "EUR",
                    "counterpart_name": "REWE GmbH",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX005",
                },
                {
                    "date": "2026-04-03",
                    "booking_date": "2026-04-03",
                    "value_date": "2026-04-03",
                    "type": "Lastschrift",
                    "text": "Strom April",
                    "amount": -75.00,
                    "currency": "EUR",
                    "counterpart_name": "Stadtwerke Berlin",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX006",
                },
                {
                    "date": "2026-04-05",
                    "booking_date": "2026-04-05",
                    "value_date": "2026-04-05",
                    "type": "Lastschrift",
                    "text": "Internet",
                    "amount": -39.99,
                    "currency": "EUR",
                    "counterpart_name": "Telekom",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX007",
                },
                {
                    "date": "2026-04-07",
                    "booking_date": "2026-04-07",
                    "value_date": "2026-04-07",
                    "type": "Überweisung",
                    "text": "Rückzahlung Abendessen",
                    "amount": 25.00,
                    "currency": "EUR",
                    "counterpart_name": "John Doe",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX008",
                },
            ],
            "summary": {
                "total_in": 3225.00,
                "total_out": 1090.45,
                "net": 2134.55,
                "count": 8,
            },
        },
        "tagesgeld": {
            "account_id": "ACC002",
            "transactions": [
                {
                    "date": "2026-04-01",
                    "booking_date": "2026-04-01",
                    "value_date": "2026-04-01",
                    "type": "Zinsen",
                    "text": "Zinsgutschrift Q1/2026",
                    "amount": 62.50,
                    "currency": "EUR",
                    "counterpart_name": "",
                    "counterpart_iban": "",
                    "transaction_id": "TX009",
                },
            ],
            "summary": {
                "total_in": 62.50,
                "total_out": 0.0,
                "net": 62.50,
                "count": 1,
            },
        },
        "depot": {
            "depot_id": "DEP001",
            "positions": [
                {
                    "isin": "IE00B4L5Y983",
                    "wkn": "A0RPWH",
                    "name": "iShares MSCI World",
                    "quantity": 15.0,
                    "current_price": 95.30,
                    "current_value": 1429.50,
                    "purchase_value": 1200.00,
                    "gains": 229.50,
                    "gains_percent": 19.125,
                    "currency": "EUR",
                },
                {
                    "isin": "IE00B1XNHC34",
                    "wkn": "A0RPWJ",
                    "name": "iShares Euro Gov Bond",
                    "quantity": 20.0,
                    "current_price": 110.50,
                    "current_value": 2210.00,
                    "purchase_value": 2300.00,
                    "gains": -90.00,
                    "gains_percent": -3.913,
                    "currency": "EUR",
                },
            ],
            "transactions": [
                {
                    "date": "2026-03-15",
                    "isin": "IE00B4L5Y983",
                    "wkn": "A0RPWH",
                    "name": "iShares MSCI World",
                    "transaction_type": "BUY",
                    "quantity": 5.0,
                    "price": 93.00,
                    "amount": -465.00,
                    "currency": "EUR",
                    "transaction_id": "DTX001",
                },
            ],
            "summary": {
                "total_value": 3639.50,
                "total_purchase_value": 3500.00,
                "total_gains": 139.50,
                "total_gains_percent": 3.9857,
                "position_count": 2,
            },
        },
        "meta": {
            "exported_at": "2026-04-10T08:00:00+00:00",
            "account_count": 2,
            "total_transaction_count": 9,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a Markdown financial report from Finance Agent JSON"
    )
    parser.add_argument(
        "--input",
        "-i",
        help="Path to Finance Agent JSON file",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Path for the output Markdown file (default: stdout)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use built-in demo data instead of a JSON file",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override report date (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    if args.demo:
        data = _build_demo_data()
    elif args.input:
        path = Path(args.input)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"Error: invalid JSON in {path}: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print("Error: JSON root must be an object", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: specify --input FILE or --demo", file=sys.stderr)
        sys.exit(1)

    try:
        report = generate_report(data, report_date=args.date)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"Report written to {out_path}")
    else:
        print(report)


if __name__ == "__main__":
    main()
