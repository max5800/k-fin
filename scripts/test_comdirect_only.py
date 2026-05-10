#!/usr/bin/env python
"""
Comdirect-only test — kein Firefly nötig.

Testet den vollständigen Auth-Flow und gibt Konten + Umsätze aus.
Ideal für ersten lokalen Test ohne laufende Firefly-Instanz.

Usage:
    uv run python scripts/test_comdirect_only.py

Lies Credentials aus .env (oder Umgebungsvariablen).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.external.comdirect_client import ComdirectClient  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402

TRANSACTIONS_LIMIT = 10  # Nur die letzten N Buchungen pro Konto ausgeben


def print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


async def main() -> None:
    setup_logging()

    print_section("Comdirect Standalone Test 🏦")
    print(f"  Client ID  : {settings.comdirect_client_id[:4]}…")
    print(f"  Username   : {settings.comdirect_username[:3]}***")
    print(f"  TAN method : {settings.comdirect_tan_method}")

    client = ComdirectClient()

    # ── Auth ────────────────────────────────────────────────────────────
    print_section("Schritt 1: Authentifizierung")
    print("  → Starte Auth-Flow (6 Schritte)…")
    print("  → Bei pushTAN: Bitte in der Comdirect App bestätigen!\n")

    success = await client.authenticate_full()
    if not success:
        print("\n[FEHLER] Auth fehlgeschlagen — Details in den Logs oben.")
        sys.exit(1)

    print(f"\n  ✅ Auth erfolgreich! (is_authenticated={client.is_authenticated})")

    # ── Konten ──────────────────────────────────────────────────────────
    print_section("Schritt 2: Konten abrufen")
    try:
        accounts = await client.get_accounts()
    except Exception as exc:
        print(f"[FEHLER] get_accounts: {exc}")
        sys.exit(1)

    if not accounts:
        print("  Keine Konten gefunden.")
        return

    print(f"  {len(accounts)} Konto(en) gefunden:\n")
    account_ids = []
    for acc in accounts:
        account_id = acc.get("account", {}).get("accountId") or acc.get("accountId", "?")
        iban = acc.get("account", {}).get("iban") or acc.get("iban", "—")
        account_type = acc.get("account", {}).get("accountType", {})
        type_text = (
            account_type.get("text", "") if isinstance(account_type, dict) else str(account_type)
        )
        # Mask IBAN for display (show first 8 + last 4)
        iban_display = f"{iban[:8]}…{iban[-4:]}" if len(iban) > 12 else iban

        print(f"  • [{type_text}] IBAN: {iban_display}")
        print(f"    Account-ID: {account_id}  |  Saldo: ***")
        print()

        if account_id != "?":
            account_ids.append((account_id, iban_display))

    # ── Umsätze ─────────────────────────────────────────────────────────
    print_section("Schritt 3: Umsätze pro Konto")

    for account_id, iban_display in account_ids:
        print(f"\n  Konto {iban_display} ({account_id}):")
        try:
            transactions = await client.get_transactions(account_id)
        except Exception as exc:
            print(f"    [FEHLER] {exc}")
            continue

        if not transactions:
            print("    Keine Umsätze gefunden.")
            continue

        for tx in transactions:
            booking_date = tx.get("bookingDate", "?")
            tx_type = tx.get("transactionType", {})
            tx_type_text = tx_type.get("text", "?") if isinstance(tx_type, dict) else str(tx_type)
            amount = tx.get("amount", {})
            amount_val = amount.get("value", "?") if isinstance(amount, dict) else amount
            currency = amount.get("unit", "EUR") if isinstance(amount, dict) else "EUR"
            remittance = tx.get("remittanceInfo", "")[:60]

            sign = "+" if str(amount_val).startswith("-") is False and amount_val != "?" else ""
            print(f"    {booking_date}  {sign}{amount_val} {currency}  [{tx_type_text}]")
            if remittance:
                print(f"              {remittance}")

    # ── Depots ──────────────────────────────────────────────────────────
    print_section("Schritt 4: Depots abrufen (optional)")
    try:
        depots = await client.get_depots()
        if depots:
            print(f"  {len(depots)} Depot(s) gefunden:")
            for depot in depots:
                depot_id = depot.get("depotId", "?")
                depot_display = depot.get("depotDisplayId", depot_id)
                print(f"  • Depot {depot_display} (ID: {depot_id})")
        else:
            print("  Keine Depots gefunden.")
    except Exception as exc:
        print(f"  [FEHLER] get_depots: {exc}")

    print_section("✅ Test abgeschlossen")
    print("  Alles sieht gut aus — Comdirect-Verbindung funktioniert!")
    print("  Nächster Schritt: Firefly III aufsetzen und End-to-End testen.\n")


if __name__ == "__main__":
    asyncio.run(main())
