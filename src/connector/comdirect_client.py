"""
Comdirect REST API client — read-only.

Auth flow:
1. POST /oauth/token → access_token (password grant)
2. POST /session/clients/user/v1/sessions → session object
3. POST /api/session/clients/user/v1/sessions/{id}/validate → trigger TAN
4. User confirms TAN (pushTAN / photoTAN / smsTAN)
5. PATCH /api/session/clients/user/v1/sessions/{id}/tan → activate session
6. POST /oauth/token → secondary access_token (cd_secondary grant)

After that, account/transaction endpoints are available.
"""

import httpx
from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger("comdirect")

BASE_URL = "https://api.comdirect.de"
OAUTH_URL = f"{BASE_URL}/oauth/token"


class ComdirectClient:
    def __init__(self):
        self.client_id = settings.comdirect_client_id
        self.client_secret = settings.comdirect_client_secret
        self.username = settings.comdirect_username
        self.pin = settings.comdirect_pin
        self.access_token: str | None = None
        self.session_id: str | None = None

    async def authenticate(self) -> bool:
        """
        Step 1 of the auth flow: password grant.
        Returns True if successful.
        Note: Full auth requires TAN confirmation (step 2-5).
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                OAUTH_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "username": self.username,
                    "password": self.pin,
                    "grant_type": "password",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token")
                logger.info("Step 1 auth OK — access_token received")
                return True
            else:
                logger.error(f"Step 1 auth failed: {response.status_code} {response.text[:300]}")
                return False

    async def get_accounts(self) -> list[dict]:
        """Fetch all accounts (Giro, Tagesgeld, etc.)."""
        if not self.access_token:
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/banking/clients/user/v2/accounts/balances",
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            return data.get("values", [])

    async def get_transactions(self, account_id: str, limit: int = 100) -> list[dict]:
        """Fetch transactions for a single account."""
        if not self.access_token:
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/banking/v1/accounts/{account_id}/transactions",
                headers=self._auth_headers(),
                params={"paging-count": limit},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("values", [])

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "x-http-request-info": '{"clientRequestId":{"sessionId":"","requestId":""}}',
            "Accept": "application/json",
        }
