"""Firefly III API client — pushes transactions."""

import httpx
from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger("firefly")


class FireflyClient:
    def __init__(self):
        self.base_url = settings.firefly_base_url.rstrip("/")
        self.token = settings.firefly_access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.api+json",
        }

    async def get_accounts(self) -> list[dict]:
        """List all accounts in Firefly III."""
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/v1/accounts", headers=self._headers())
            r.raise_for_status()
            return r.json().get("data", [])

    async def find_account_by_iban(self, iban: str) -> dict | None:
        """Find a Firefly account by IBAN."""
        accounts = await self.get_accounts()
        for acc in accounts:
            attrs = acc.get("attributes", {})
            if attrs.get("iban") == iban:
                return acc
        return None

    async def create_transaction(self, payload: dict) -> dict:
        """Create a single transaction in Firefly III."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/api/v1/transactions",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    async def transaction_exists(self, external_id: str) -> bool:
        """Check if a transaction with a given external_id already exists (dedup)."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.base_url}/api/v1/transactions",
                headers=self._headers(),
                params={"external_id": external_id},
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                return len(data) > 0
        return False
