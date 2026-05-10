#!/usr/bin/env python
"""
Interactive test script for the full Comdirect OAuth auth flow.

Usage:
    uv run python scripts/test_auth.py

Reads credentials from .env (or environment variables).
On success, prints account balances.
"""

import asyncio
import sys
from pathlib import Path

# Allow imports from the project root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import settings  # noqa: E402 — needs sys.path fix above
from src.core.logging import get_logger, setup_logging  # noqa: E402
from src.external.comdirect_client import ComdirectClient  # noqa: E402

logger = get_logger("test_auth")


async def main() -> None:
    setup_logging()

    print("=== Comdirect full auth flow test ===")
    print(f"Client ID : {settings.comdirect_client_id[:4]}…")
    print(f"Username  : {settings.comdirect_username[:3]}***")
    print(f"TAN method: {settings.comdirect_tan_method}")
    print()

    client = ComdirectClient()

    success = await client.authenticate_full()
    if not success:
        print("\n[ERROR] Authentication failed — check logs above.")
        sys.exit(1)

    print("\n[OK] Authentication successful!")
    print(f"  is_authenticated = {client.is_authenticated}")
    print()

    print("Fetching account balances…")
    try:
        accounts = await client.get_accounts()
    except Exception as exc:
        print(f"[ERROR] get_accounts failed: {exc}")
        sys.exit(1)

    if not accounts:
        print("No accounts returned.")
        return

    print(f"Found {len(accounts)} account(s):\n")
    for acc in accounts:
        # The exact shape depends on the API; handle gracefully.
        account_id = acc.get("account", {}).get("accountId", acc.get("accountId", "?"))
        iban = acc.get("account", {}).get("iban", acc.get("iban", ""))
        iban_masked = f"{iban[:8]}***{iban[-4:]}" if len(iban) > 12 else "***"
        print(f"  Account {account_id}  IBAN: {iban_masked}  Balance: ***")


if __name__ == "__main__":
    asyncio.run(main())
