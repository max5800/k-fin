#!/usr/bin/env python
"""
CLI — Vollständiger Comdirect Finanz-Export als CSV.

Exportiert:
  1. Konto-Umsätze (pro Konto eine CSV)
  2. Depot-Positionen (aktuelle Holdings)
  3. Depot-Transaktionen (Käufe/Verkäufe/Dividenden)
  4. Übersicht (Kontostände + Depot-Gesamtwert)

Usage:
    uv run python scripts/export_csv.py [--output-dir DIR] [--since 2025-01-01] [--since 30d]
"""

import argparse
import asyncio
import csv
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from src.connector.comdirect_client import ComdirectClient
from src.core.config import settings  # noqa: F401
from src.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger("export_csv_cli")


def parse_since(since_str: str | None) -> date | None:
    """Parst --since Argument: YYYY-MM-DD oder Xd (z.B. 30d = 30 Tage zurück)."""
    if not since_str:
        return None
    m = re.fullmatch(r"(\d+)d", since_str)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(since_str)
    except ValueError:
        raise ValueError(
            f"Ungültiges --since Format: '{since_str}'. "
            "Erwartet: YYYY-MM-DD oder Xd (z.B. 30d)"
        )


def filter_transactions_by_date(transactions: list[dict], since: date) -> list[dict]:
    """Filtert Transaktionen client-seitig nach bookingDate >= since.

    Offene (nicht gebuchte) Transaktionen werden immer beibehalten.
    """
    since_str = since.isoformat()
    filtered = []
    for tx in transactions:
        booking = tx.get("bookingDate", "")
        if not booking or booking == "offen":
            filtered.append(tx)  # offene immer mitnehmen
        elif booking >= since_str:
            filtered.append(tx)
    return filtered


def format_date(d_str: str) -> str:
    """Konvertiert YYYY-MM-DD zu DD.MM.YYYY. Bei fehlendem Wert 'offen' oder '--'."""
    if not d_str or d_str == "?" or d_str == "offen":
        return "offen"
    parts = d_str.split("-")
    if len(parts) >= 3:
        # Falls Zeit angehängt ist (z.B. YYYY-MM-DDTHH:MM:SS)
        day = parts[2][:2]
        return f"{day}.{parts[1]}.{parts[0]}"
    return d_str


def format_amount(val) -> str:
    """Konvertiert -21.87 zu -21,87"""
    try:
        f_val = float(val)
        return f"{f_val:.2f}".replace(".", ",")
    except (ValueError, TypeError):
        return str(val).replace(".", ",")


def build_buchungstext(tx: dict) -> str:
    """
    Baut den Buchungstext zusammen.
    Das Original-CSV enthält teils Auftraggeber und Buchungstext in einer Spalte.
    """
    remittance = tx.get("remittanceInfo", "")
    creditor_obj = tx.get("creditor") or {}
    debtor_obj = tx.get("debtor") or {}
    creditor = (
        creditor_obj.get("name", "")
        if isinstance(creditor_obj, dict)
        else str(creditor_obj)
    )
    debtor = (
        debtor_obj.get("name", "") if isinstance(debtor_obj, dict) else str(debtor_obj)
    )

    parts = []

    if creditor:
        parts.append(f"Auftraggeber: {creditor}")
    elif debtor:
        parts.append(f"Empfänger: {debtor}")

    # Manche Transaktionen haben IBANs/BICs im Dictionary
    creditor_iban = (
        creditor_obj.get("iban", "") if isinstance(creditor_obj, dict) else ""
    )
    debtor_iban = debtor_obj.get("iban", "") if isinstance(debtor_obj, dict) else ""
    if creditor_iban:
        parts.append(f"Kto/IBAN: {creditor_iban}")
    elif debtor_iban:
        parts.append(f"Kto/IBAN: {debtor_iban}")

    if remittance:
        parts.append(f"Buchungstext: {remittance}")

    return " ".join(parts)


def export_account_to_csv(
    account_id: str,
    iban: str,
    balance_info: dict,
    transactions: list[dict],
    output_dir: Path,
    since: date | None = None,
):
    """
    Schreibt die Umsätze eines Kontos in eine CSV-Datei.
    Format orientiert sich am Comdirect Web-Export.
    """
    today_str = date.today().strftime("%Y%m%d")
    since_str = since.strftime("%Y%m%d") if since else None
    suffix = f"{since_str}-{today_str}" if since_str else today_str
    filename = output_dir / f"umsaetze_{iban}_{suffix}.csv"

    # Kontostand extrahieren
    balance_val = balance_info.get("value", "?")
    balance_unit = balance_info.get("unit", "EUR")

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL)

        # Kopfzeilen mit Kontostand (wie im Comdirect Web-Export)
        account_type = balance_info.get("account_type", "Girokonto")
        writer.writerow([f"Umsätze {account_type}", f"Konto: {iban}"])
        writer.writerow(
            ["Neuer Kontostand", f"{format_amount(balance_val)} {balance_unit}"]
        )
        writer.writerow([])

        # Spaltenköpfe
        writer.writerow(
            [
                "Buchungstag",
                "Wertstellung (Valuta)",
                "Vorgang",
                "Buchungstext",
                "Umsatz in EUR",
            ]
        )

        for tx in transactions:
            b_date = format_date(tx.get("bookingDate", ""))
            v_date = tx.get("valutaDate", "")
            v_date_formatted = format_date(v_date) if v_date else "--"

            tx_type_obj = tx.get("transactionType", {})
            vorgang = (
                tx_type_obj.get("text", "")
                if isinstance(tx_type_obj, dict)
                else str(tx_type_obj)
            )

            buchungstext = build_buchungstext(tx)

            amount_obj = tx.get("amount", {})
            amount_val = (
                amount_obj.get("value", "0.0")
                if isinstance(amount_obj, dict)
                else amount_obj
            )
            umsatz_eur = format_amount(amount_val)

            writer.writerow(
                [b_date, v_date_formatted, vorgang, buchungstext, umsatz_eur]
            )

    logger.info(f"Konto {iban}: {len(transactions)} Umsätze → {filename.name}")


def export_depot_positions_csv(depot_id: str, positions: list[dict], output_dir: Path):
    """Exportiert aktuelle Depot-Positionen (Holdings) als CSV."""
    date_str = date.today().strftime("%Y%m%d")
    filename = output_dir / f"depot_positionen_{date_str}.csv"

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL)

        writer.writerow(["Depot-Positionen", f"Depot: {depot_id}"])
        writer.writerow([f"Stand: {date.today().strftime('%d.%m.%Y')}"])
        writer.writerow([])

        writer.writerow(
            [
                "ISIN",
                "WKN",
                "Bezeichnung",
                "Stückzahl",
                "Kurs",
                "Kurs-Währung",
                "Aktueller Wert (EUR)",
                "Einstandswert (EUR)",
                "Gewinn/Verlust (EUR)",
                "Gewinn/Verlust (%)",
            ]
        )

        total_value = 0.0
        total_purchase = 0.0

        for pos in positions:
            instrument = pos.get("instrument") or {}
            isin = instrument.get("isin") or pos.get("isin", "")
            wkn = instrument.get("wkn") or pos.get("wkn", "")
            name = instrument.get("name") or pos.get("name", "")

            quantity_obj = pos.get("quantity") or {}
            quantity = float(
                quantity_obj.get("value", 0)
                if isinstance(quantity_obj, dict)
                else quantity_obj or 0
            )

            price_obj = pos.get("currentPrice") or pos.get("kurs") or {}
            price = float(
                price_obj.get("price", {}).get("value", 0)
                if isinstance(price_obj.get("price"), dict)
                else price_obj.get("value", 0)
            )
            price_currency = (
                price_obj.get("price", {}).get("unit", "")
                if isinstance(price_obj.get("price"), dict)
                else price_obj.get("unit", "EUR")
            )

            current_val_obj = pos.get("currentValue") or pos.get("kurswert") or {}
            current_val = float(
                current_val_obj.get("value", 0)
                if isinstance(current_val_obj, dict)
                else current_val_obj or 0
            )

            purchase_val_obj = (
                pos.get("purchaseValue") or pos.get("einstandswert") or {}
            )
            purchase_val = float(
                purchase_val_obj.get("value", 0)
                if isinstance(purchase_val_obj, dict)
                else purchase_val_obj or 0
            )

            gains = round(current_val - purchase_val, 2)
            gains_pct = round((gains / purchase_val * 100) if purchase_val else 0, 2)

            total_value += current_val
            total_purchase += purchase_val

            writer.writerow(
                [
                    isin,
                    wkn,
                    name,
                    format_amount(quantity),
                    format_amount(price),
                    price_currency,
                    format_amount(current_val),
                    format_amount(purchase_val),
                    format_amount(gains),
                    format_amount(gains_pct),
                ]
            )

        # Summenzeile
        total_gains = round(total_value - total_purchase, 2)
        total_gains_pct = round(
            (total_gains / total_purchase * 100) if total_purchase else 0, 2
        )
        writer.writerow([])
        writer.writerow(
            [
                "",
                "",
                "GESAMT",
                "",
                "",
                "",
                format_amount(total_value),
                format_amount(total_purchase),
                format_amount(total_gains),
                format_amount(total_gains_pct),
            ]
        )

    logger.info(f"Depot: {len(positions)} Positionen → {filename.name}")


def export_depot_transactions_csv(
    depot_id: str,
    transactions: list[dict],
    output_dir: Path,
    since: date | None = None,
):
    """Exportiert Depot-Transaktionen (Käufe, Verkäufe, Dividenden) als CSV."""
    today_str = date.today().strftime("%Y%m%d")
    since_str = since.strftime("%Y%m%d") if since else None
    suffix = f"{since_str}-{today_str}" if since_str else today_str
    filename = output_dir / f"depot_umsaetze_{suffix}.csv"

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL)

        writer.writerow(["Depot-Umsätze", f"Depot: {depot_id}"])
        writer.writerow([])

        writer.writerow(
            [
                "Buchungstag",
                "Geschäftsart",
                "ISIN",
                "WKN",
                "Bezeichnung",
                "Stückzahl",
                "Kurs",
                "Betrag (EUR)",
            ]
        )

        for tx in transactions:
            booking_date = format_date(
                tx.get("bookingDate") or tx.get("transactionDate", "")
            )

            tx_type = tx.get("transactionType") or tx.get("transactionDirection") or ""

            instrument = tx.get("instrument") or {}
            isin = instrument.get("isin") or tx.get("isin", "")
            wkn = instrument.get("wkn") or tx.get("wkn", "")
            name = instrument.get("name") or tx.get("name", "")

            quantity_obj = tx.get("quantity") or {}
            quantity = float(
                quantity_obj.get("value", 0)
                if isinstance(quantity_obj, dict)
                else quantity_obj or 0
            )

            price_obj = tx.get("price") or tx.get("kurs") or {}
            price = float(
                price_obj.get("value", 0)
                if isinstance(price_obj, dict)
                else price_obj or 0
            )

            amount_obj = tx.get("transactionValue") or tx.get("amount") or {}
            amount = float(
                amount_obj.get("value", 0)
                if isinstance(amount_obj, dict)
                else amount_obj or 0
            )

            writer.writerow(
                [
                    booking_date,
                    tx_type,
                    isin,
                    wkn,
                    name,
                    format_amount(quantity),
                    format_amount(price),
                    format_amount(amount),
                ]
            )

    logger.info(f"Depot: {len(transactions)} Transaktionen → {filename.name}")


def export_summary_csv(
    accounts: list[dict], depot_positions: list[dict], output_dir: Path
):
    """Exportiert eine Gesamtübersicht aller Konten und Depotwerte."""
    date_str = date.today().strftime("%Y%m%d")
    filename = output_dir / f"finanzuebersicht_{date_str}.csv"

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_ALL)

        writer.writerow(
            ["Finanzübersicht", f"Stand: {date.today().strftime('%d.%m.%Y')}"]
        )
        writer.writerow([])
        writer.writerow(["Typ", "Bezeichnung", "IBAN / Depot-Nr.", "Saldo (EUR)"])

        total = 0.0

        # Konten
        for acc in accounts:
            account_inner = acc.get("account") or acc
            iban = account_inner.get("iban", "")
            acc_type_obj = account_inner.get("accountType") or {}
            acc_type = (
                acc_type_obj.get("text", "Konto")
                if isinstance(acc_type_obj, dict)
                else str(acc_type_obj)
            )

            balance_obj = acc.get("balance") or {}
            balance = float(
                balance_obj.get("value", 0)
                if isinstance(balance_obj, dict)
                else balance_obj or 0
            )
            total += balance

            writer.writerow(
                [
                    "Konto",
                    acc_type,
                    iban,
                    format_amount(balance),
                ]
            )

        # Depot
        if depot_positions:
            depot_value = 0.0
            for pos in depot_positions:
                val_obj = pos.get("currentValue") or pos.get("kurswert") or {}
                depot_value += float(
                    val_obj.get("value", 0)
                    if isinstance(val_obj, dict)
                    else val_obj or 0
                )
            total += depot_value

            writer.writerow(
                [
                    "Depot",
                    "Wertpapierdepot",
                    "",
                    format_amount(depot_value),
                ]
            )

        # Gesamtsumme
        writer.writerow([])
        writer.writerow(["", "GESAMT", "", format_amount(total)])

    logger.info(f"Übersicht → {filename.name}")


async def _fetch_all_transactions(
    client: ComdirectClient, account_id: str
) -> list[dict]:
    """Holt alle Transaktionen eines Kontos in einem Request.

    Die API unterstützt kein paging-first > 0, daher nutzen wir
    einen einzelnen Request mit hohem paging-count.
    """
    try:
        return await asyncio.wait_for(
            client.get_transactions(account_id),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        logger.warning("  Timeout beim Abruf — breche ab.")
        return []
    except Exception as exc:
        logger.warning(f"  Fehler beim Abruf: {exc} — breche ab.")
        return []


async def _run(output_dir: str, since: date | None = None):
    client = ComdirectClient()

    logger.info("Starte Comdirect Auth-Flow…")
    try:
        ok = await client.authenticate_full()
    except Exception as exc:
        logger.error(f"Auth fehlgeschlagen: {exc}")
        sys.exit(1)

    if not ok:
        logger.error("Authentication failed. Check credentials or TAN.")
        sys.exit(1)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # ── 1. Konten + Umsätze ────────────────────────────────────────────
    logger.info("Hole Konten…")
    accounts = await client.get_accounts()
    if not accounts:
        logger.warning("Keine Konten gefunden.")
    else:
        logger.info(f"{len(accounts)} Konten gefunden.")

    for acc in accounts:
        account_inner = acc.get("account") or acc
        account_id = account_inner.get("accountId")
        iban = account_inner.get("iban", account_id)
        if not account_id:
            continue

        # Kontostand + Typ für CSV-Header
        balance_obj = acc.get("balance") or {}
        acc_type_obj = account_inner.get("accountType") or {}
        acc_type_text = (
            acc_type_obj.get("text", "Girokonto")
            if isinstance(acc_type_obj, dict)
            else str(acc_type_obj)
        )
        balance_info = {
            "value": (
                balance_obj.get("value", "0")
                if isinstance(balance_obj, dict)
                else str(balance_obj)
            ),
            "unit": (
                balance_obj.get("unit", "EUR")
                if isinstance(balance_obj, dict)
                else "EUR"
            ),
            "account_type": acc_type_text,
        }

        logger.info(f"Hole Umsätze für {acc_type_text} {iban} …")
        all_txs = await _fetch_all_transactions(client, account_id)
        if since:
            all_txs = filter_transactions_by_date(all_txs, since)
        logger.info(f"  → {len(all_txs)} Umsätze")
        export_account_to_csv(
            account_id, iban, balance_info, all_txs, out_path, since=since
        )

    # ── 2. Depot ────────────────────────────────────────────────────────
    logger.info("Hole Depots…")
    depots = await client.get_depots()

    all_depot_positions: list[dict] = []
    for depot in depots:
        depot_id = depot.get("depotId")
        if not depot_id:
            continue

        logger.info(f"Hole Depot-Positionen für {depot_id} …")
        positions = await client.get_depot_positions(depot_id)
        all_depot_positions.extend(positions)
        logger.info(f"  → {len(positions)} Positionen")
        export_depot_positions_csv(depot_id, positions, out_path)

        logger.info(f"Hole Depot-Transaktionen für {depot_id} …")
        min_booking = since.isoformat() if since else None
        depot_txs = await client.get_depot_transactions(
            depot_id, min_booking_date=min_booking
        )
        logger.info(f"  → {len(depot_txs)} Transaktionen")
        export_depot_transactions_csv(depot_id, depot_txs, out_path, since=since)

    # ── 3. Gesamtübersicht ──────────────────────────────────────────────
    export_summary_csv(accounts, all_depot_positions, out_path)

    logger.info("✅ Vollständiger Finanz-Export abgeschlossen.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Vollständiger Comdirect Finanz-Export (Konten, Depot, Übersicht)"
    )
    parser.add_argument(
        "--output-dir", default="exports", help="Directory to save the CSV files"
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Nur Umsätze ab diesem Datum. Format: YYYY-MM-DD oder Xd (z.B. 30d, 90d)",
    )
    args = parser.parse_args()

    since = parse_since(args.since) if args.since else None
    if since:
        logger.info(f"Zeitraum-Filter: ab {since.isoformat()}")

    asyncio.run(_run(args.output_dir, since=since))
