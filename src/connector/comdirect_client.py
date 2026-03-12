"""
Comdirect REST API client — read-only.

Auth flow:
1. POST /oauth/token → access_token (password grant)
2. POST /api/session/clients/user/v1/sessions → session object
3. POST /api/session/clients/user/v1/sessions/{sessionId}/validate → trigger TAN
4. User confirms TAN (pushTAN / photoTAN / smsTAN)
5. PATCH /api/session/clients/user/v1/sessions/{sessionId}/tan → activate session
6. POST /oauth/token → secondary access_token (cd_secondary grant)

After that, account/transaction endpoints are available.
"""

import asyncio
import json
import uuid

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
        self._primary_token: str | None = None
        self._secondary_token: str | None = None
        self.session_id: str = str(uuid.uuid4())

        # Legacy alias kept for callers that read .access_token directly
        self.access_token: str | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        """True only after the full 6-step flow has completed."""
        return self._secondary_token is not None

    # ------------------------------------------------------------------
    # Header helpers
    # ------------------------------------------------------------------

    def _request_info_header(self) -> str:
        """Build the x-http-request-info JSON value."""
        return json.dumps(
            {
                "clientRequestId": {
                    "sessionId": self.session_id,
                    "requestId": uuid.uuid4().hex[:10],
                }
            }
        )

    def _auth_headers(self) -> dict:
        """Return auth headers using the best available token."""
        token = self._secondary_token or self._primary_token or self.access_token
        return {
            "Authorization": f"Bearer {token}",
            "x-http-request-info": self._request_info_header(),
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Step 1 only (legacy / minimal)
    # ------------------------------------------------------------------

    async def authenticate(self) -> bool:
        """
        Step 1 of the auth flow: password grant.
        Returns True if successful.
        Note: Full auth requires TAN confirmation (steps 2–6).
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
                self._primary_token = data.get("access_token")
                self.access_token = self._primary_token  # legacy alias
                logger.info("Step 1 auth OK — access_token received")
                return True
            else:
                logger.error(
                    f"Step 1 auth failed: {response.status_code} {response.text[:300]}"
                )
                return False

    # ------------------------------------------------------------------
    # Full 6-step auth flow
    # ------------------------------------------------------------------

    async def authenticate_full(self) -> bool:
        """
        Full Comdirect OAuth auth flow (steps 1–6).

        Steps 1–3 are automated; step 4 blocks until the user confirms the
        TAN in their app and presses Enter.  Steps 5–6 then complete the
        session activation and obtain the secondary access token required
        for all data API calls.

        Returns True on success, False on any error.
        """
        async with httpx.AsyncClient() as http:
            # ---- Step 1: Password grant --------------------------------
            logger.info("Step 1: Requesting primary access token (password grant)…")
            resp = await http.post(
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
            if resp.status_code != 200:
                logger.error(
                    f"Step 1 failed: {resp.status_code} {resp.text[:300]}"
                )
                return False

            token_data = resp.json()
            self._primary_token = token_data["access_token"]
            self.access_token = self._primary_token  # legacy alias
            kdnr = token_data.get("kdnr", self.username)
            logger.info(f"Step 1 OK — kdnr={kdnr}")

            # ---- Step 2: Create session ---------------------------------
            logger.info("Step 2: Creating session…")
            resp = await http.post(
                f"{BASE_URL}/api/session/clients/user/v1/sessions",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json",
                },
                json={"identifier": self._primary_token},
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    f"Step 2 failed: {resp.status_code} {resp.text[:300]}"
                )
                return False

            session_data = resp.json()
            # If the API returns a list, unwrap the first element.
            if isinstance(session_data, list):
                session_data = session_data[0] if session_data else {}
            session_identifier = session_data.get("identifier", self.session_id)
            logger.info(f"Step 2 OK — session identifier={session_identifier}")

            # ---- Step 3: Validate session (triggers TAN) ---------------
            logger.info("Step 3: Validating session — TAN will be sent to device…")
            resp = await http.post(
                f"{BASE_URL}/api/session/clients/user/v1/sessions/{session_identifier}/validate",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json",
                },
                json={"identifier": self._primary_token},
            )
            if resp.status_code not in (200, 201, 202):
                logger.error(
                    f"Step 3 failed: {resp.status_code} {resp.text[:300]}"
                )
                return False

            # Extract the challenge info returned by the server.
            tan_info_raw = resp.headers.get("x-once-authentication-info", "{}")
            try:
                tan_info = json.loads(tan_info_raw)
            except json.JSONDecodeError:
                tan_info = {}

            challenge_id = tan_info.get("id", "")
            tan_typ = tan_info.get("typ", settings.comdirect_tan_method)
            logger.info(
                f"Step 3 OK — TAN challenge id={challenge_id!r}, typ={tan_typ!r}"
            )

            # ---- Step 4: Wait for user to confirm TAN ------------------
            print(f"\nTAN method: {tan_typ}")
            print("Please confirm the TAN in your Comdirect app, then press Enter…")
            # run_in_executor so we don't block the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, input)
            logger.info("Step 4: User confirmed TAN")

            # ---- Step 5: Activate session with TAN ---------------------
            logger.info("Step 5: Activating session…")
            resp = await http.patch(
                f"{BASE_URL}/api/session/clients/user/v1/sessions/{session_identifier}/tan",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json",
                    # Echo back the challenge id; TAN value empty for pushTAN
                    # (the user confirmed in-app rather than typing a code).
                    "x-once-authentication-info": json.dumps({"id": challenge_id}),
                    "x-once-authentication": "",
                },
                json={"identifier": self._primary_token},
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    f"Step 5 failed: {resp.status_code} {resp.text[:300]}"
                )
                return False
            logger.info("Step 5 OK — session activated")

            # ---- Step 6: Secondary token (cd_secondary grant) ----------
            logger.info("Step 6: Obtaining secondary access token…")
            resp = await http.post(
                OAUTH_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "cd_secondary",
                    "token": self._primary_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                logger.error(
                    f"Step 6 failed: {resp.status_code} {resp.text[:300]}"
                )
                return False

            self._secondary_token = resp.json()["access_token"]
            self.access_token = self._secondary_token  # legacy alias
            logger.info("Step 6 OK — secondary token received, auth complete")
            return True

    # ------------------------------------------------------------------
    # Data API methods (require full auth / secondary token)
    # ------------------------------------------------------------------

    async def get_accounts(self) -> list[dict]:
        """Fetch all accounts (Giro, Tagesgeld, etc.)."""
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/banking/clients/user/v2/accounts/balances",
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            return data.get("values", [])

    async def get_transactions(self, account_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch transactions for a single account."""
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/banking/v1/accounts/{account_id}/transactions",
                headers=self._auth_headers(),
                params={"paging-count": limit, "paging-first-index": offset},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("values", [])

    async def get_depots(self) -> list[dict]:
        """Fetch all depots (brokerage accounts)."""
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/brokerage/clients/user/v3/depots",
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"get_depots: {len(data.get('values', []))} depots found")
            return data.get("values", [])

    async def get_depot_positions(self, depot_id: str) -> list[dict]:
        """Fetch current holdings for a depot (ISIN, WKN, quantity, market value)."""
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/brokerage/v3/depots/{depot_id}/positions",
                headers=self._auth_headers(),
                params={"without-attr": "depot,benchmarkComparison"},
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"get_depot_positions({depot_id}): {len(data.get('values', []))} positions")
            return data.get("values", [])

    async def get_depot_transactions(self, depot_id: str, limit: int = 100) -> list[dict]:
        """Fetch securities transactions for a depot (buys, sells, dividends)."""
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/brokerage/v3/depots/{depot_id}/transactions",
                headers=self._auth_headers(),
                params={"paging-count": limit},
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"get_depot_transactions({depot_id}): {len(data.get('values', []))} transactions")
            return data.get("values", [])

    async def get_all_data(self) -> dict:
        """
        Fetch all accounts, transactions, depots, positions, and depot transactions.

        Returns:
        {
            "accounts": [...],
            "transactions": { "{accountId}": [...] },
            "depots": [...],
            "depot_positions": { "{depotId}": [...] },
            "depot_transactions": { "{depotId}": [...] },
        }
        """
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        logger.info("get_all_data: fetching accounts and depots…")
        accounts = await self.get_accounts()
        depots = await self.get_depots()

        transactions: dict[str, list[dict]] = {}
        for account in accounts:
            account_id = account.get("account", {}).get("accountId") or account.get("accountId")
            if account_id:
                logger.info(f"get_all_data: fetching transactions for account {account_id}")
                transactions[account_id] = await self.get_transactions(account_id)

        depot_positions: dict[str, list[dict]] = {}
        depot_transactions: dict[str, list[dict]] = {}
        for depot in depots:
            depot_id = depot.get("depotId")
            if depot_id:
                logger.info(f"get_all_data: fetching positions/transactions for depot {depot_id}")
                depot_positions[depot_id] = await self.get_depot_positions(depot_id)
                depot_transactions[depot_id] = await self.get_depot_transactions(depot_id)

        logger.info("get_all_data: complete")
        return {
            "accounts": accounts,
            "transactions": transactions,
            "depots": depots,
            "depot_positions": depot_positions,
            "depot_transactions": depot_transactions,
        }
