"""
Comdirect REST API client — read-only.

Two-step auth flow:
1. begin_auth() — steps 1-3: obtain token, create session, trigger TAN challenge
2. User confirms TAN in banking app
3. complete_auth() — steps 4-6: activate session, obtain secondary token

After that, account/transaction endpoints are available.
"""

import json
import re
import uuid
from datetime import date, timedelta

import httpx

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger("comdirect")

BASE_URL = "https://api.comdirect.de"
OAUTH_URL = f"{BASE_URL}/oauth/token"

_RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")


def resolve_booking_date(value: str | None) -> str | None:
    """Convert a booking-date value to YYYY-MM-DD if it uses relative notation.

    Accepted formats:
    - ``None`` → ``None`` (no filter)
    - ``"2025-01-15"`` → ``"2025-01-15"`` (absolute, passed through)
    - ``"-30d"`` → today minus 30 days as ``YYYY-MM-DD``
    """
    if not value:
        return None
    m = _RELATIVE_DATE_RE.match(value)
    if m:
        days = int(m.group(1))
        if days > 3650:
            raise ValueError(
                f"Relative booking date exceeds maximum of 3650 days: {days}"
            )
        return (date.today() - timedelta(days=days)).isoformat()
    return value


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
            "User-Agent": "k-fin/1.0",
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
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "k-fin/1.0",
                },
            )
            if response.status_code == 200:
                data = response.json()
                self._primary_token = data.get("access_token")
                self.access_token = self._primary_token  # legacy alias
                logger.info("Step 1 auth OK — access_token received")
                return True
            else:
                logger.error(f"Step 1 auth failed: HTTP {response.status_code}")
                return False

    # ------------------------------------------------------------------
    # Two-step auth flow
    # ------------------------------------------------------------------

    async def begin_auth(self) -> dict:
        """Steps 1–3: obtain token, create session, trigger TAN challenge.

        Returns {"session_identifier": ..., "challenge_id": ...} on success.
        Raises RuntimeError on failure.
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
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "k-fin/1.0",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Step 1 failed: HTTP {resp.status_code}")

            token_data = resp.json()
            self._primary_token = token_data["access_token"]
            self.access_token = self._primary_token  # legacy alias
            logger.info("Step 1 OK — primary token received")

            # ---- Step 2: Get session ---------------------------------
            logger.info("Step 2: Getting session…")
            resp = await http.get(
                f"{BASE_URL}/api/session/clients/user/v1/sessions",
                headers={
                    **self._auth_headers(),
                    "Accept": "application/json",
                    "User-Agent": "k-fin/1.0",
                },
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Step 2 failed: HTTP {resp.status_code}")

            session_data = resp.json()
            if isinstance(session_data, list):
                session_data = session_data[0] if session_data else {}
            session_identifier = session_data.get("identifier", self.session_id)
            self.session_id = session_identifier
            logger.info("Step 2 OK — session acquired")

            # ---- Step 3: Validate session (triggers TAN) ---------------
            logger.info("Step 3: Validating session — TAN will be sent to device…")
            validate_body = {
                "identifier": session_identifier,
                "sessionTanActive": True,
                "activated2FA": True,
            }

            resp = await http.post(
                f"{BASE_URL}/api/session/clients/user/v1/sessions/{session_identifier}/validate",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json",
                },
                json=validate_body,
            )
            if resp.status_code not in (200, 201, 202):
                raise RuntimeError(f"Step 3 failed: HTTP {resp.status_code}")

            tan_info_raw = resp.headers.get("x-once-authentication-info", "{}")
            try:
                tan_info = json.loads(tan_info_raw)
            except json.JSONDecodeError:
                tan_info = {}

            challenge_id = tan_info.get("id", "")
            tan_typ = tan_info.get("typ", settings.comdirect_tan_method)
            logger.info(f"Step 3 OK — TAN challenge sent, typ={tan_typ!r}")

            return {
                "session_identifier": session_identifier,
                "challenge_id": challenge_id,
            }

    async def complete_auth(self, session_identifier: str, challenge_id: str) -> bool:
        """Steps 4–6: activate session after TAN confirmation, get secondary token.

        Call this after the user has confirmed the TAN in the banking app.
        Returns True on success, False on error.
        """
        async with httpx.AsyncClient() as http:
            # ---- Step 5: Activate session (TAN already confirmed) ------
            logger.info("Step 5: Activating session…")
            activation_body = {
                "identifier": session_identifier,
                "sessionTanActive": True,
                "activated2FA": True,
            }
            headers_step_5 = {
                **self._auth_headers(),
                "Content-Type": "application/json",
                "x-once-authentication-info": json.dumps({"id": challenge_id}),
            }

            resp = await http.patch(
                f"{BASE_URL}/api/session/clients/user/v1/sessions/{session_identifier}",
                headers=headers_step_5,
                json=activation_body,
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Step 5 failed: HTTP {resp.status_code}")
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
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._primary_token}",
                    "User-Agent": "k-fin/1.0",
                },
            )
            if resp.status_code != 200:
                logger.error(f"Step 6 failed: HTTP {resp.status_code}")
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

    async def get_transactions(
        self,
        account_id: str,
        paging_count: int = 500,
        min_booking_date: str | None = None,
    ) -> list[dict]:
        """Fetch transactions for a single account.

        The API does not support paging-first > 0, so we fetch everything
        in a single request using paging-count.

        Args:
            min_booking_date: Earliest booking date (YYYY-MM-DD or relative offset
                like ``-90d``) when supported by the API.
        """
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        resolved_date = resolve_booking_date(min_booking_date)

        params: dict[str, str | int] = {"paging-count": paging_count}
        if resolved_date:
            params["min-bookingDate"] = resolved_date

        logger.info(
            "get_transactions(%s): paging-count=%s, min-bookingDate=%s (raw=%s)",
            account_id,
            paging_count,
            resolved_date,
            min_booking_date,
        )

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/banking/v1/accounts/{account_id}/transactions",
                headers=self._auth_headers(),
                params=params,
            )
            if response.status_code >= 400:
                logger.error(
                    "get_transactions(%s) failed: HTTP %d, params=%s, body=%s",
                    account_id,
                    response.status_code,
                    params,
                    response.text[:500],
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
                params={"with-attr": "instrument"},
            )
            response.raise_for_status()
            data = response.json()
            logger.info(
                f"get_depot_positions({depot_id}): {len(data.get('values', []))} positions"
            )
            return data.get("values", [])

    async def get_depot_transactions(
        self,
        depot_id: str,
        limit: int = 100,
        min_booking_date: str | None = None,
    ) -> list[dict]:
        """Fetch securities transactions for a depot (buys, sells, dividends).

        Args:
            min_booking_date: Earliest booking date (YYYY-MM-DD or -Xd offset).
                              Defaults to API default (-180d) if not set.
        """
        if not (self._secondary_token or self.access_token):
            raise RuntimeError("Not authenticated")

        resolved_date = resolve_booking_date(min_booking_date)

        params: dict[str, str | int] = {"paging-count": limit}
        if resolved_date:
            params["min-bookingDate"] = resolved_date

        logger.info(
            "get_depot_transactions(%s): paging-count=%s, min-bookingDate=%s (raw=%s)",
            depot_id,
            limit,
            resolved_date,
            min_booking_date,
        )

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/api/brokerage/v3/depots/{depot_id}/transactions",
                headers=self._auth_headers(),
                params=params,
            )
            if response.status_code >= 400:
                logger.error(
                    "get_depot_transactions(%s) failed: HTTP %d, params=%s, body=%s",
                    depot_id,
                    response.status_code,
                    params,
                    response.text[:500],
                )
            response.raise_for_status()
            data = response.json()
            values = data.get("values", [])
            if len(values) >= limit:
                # Comdirect caps depot paging-count at 500. Without a paging
                # loop (M11 tech debt) we silently drop anything beyond — log
                # a WARN so trading-heavy depots make it visible.
                logger.warning(
                    "get_depot_transactions(%s): hit paging cap (%d rows) — "
                    "older transactions in the window may be truncated",
                    depot_id,
                    limit,
                )
            logger.info(
                f"get_depot_transactions({depot_id}): {len(values)} transactions"
            )
            return values

    async def get_all_data(
        self,
        account_transaction_limit: int = 500,
        account_transaction_min_booking_date: str | None = None,
        depot_transaction_limit: int = 100,
        depot_transaction_min_booking_date: str | None = None,
    ) -> dict:
        """
        Fetch all accounts, transactions, depots, positions, and depot transactions.

        Defaults preserve the previous behavior:
        - account transactions: up to 500 items per account, no explicit date filter
        - depot transactions: up to 100 items per depot, API default date window

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
            account_id = account.get("account", {}).get("accountId") or account.get(
                "accountId"
            )
            if account_id:
                logger.info(
                    "get_all_data: fetching transactions for account %s "
                    "(limit=%s, min_booking_date=%s)",
                    account_id,
                    account_transaction_limit,
                    account_transaction_min_booking_date,
                )
                txs = await self.get_transactions(
                    account_id,
                    paging_count=account_transaction_limit,
                    min_booking_date=account_transaction_min_booking_date,
                )
                transactions[account_id] = txs

        depot_positions: dict[str, list[dict]] = {}
        depot_transactions: dict[str, list[dict]] = {}
        for depot in depots:
            depot_id = depot.get("depotId")
            if depot_id:
                logger.info(
                    "get_all_data: fetching positions/transactions for depot %s "
                    "(limit=%s, min_booking_date=%s)",
                    depot_id,
                    depot_transaction_limit,
                    depot_transaction_min_booking_date,
                )
                depot_positions[depot_id] = await self.get_depot_positions(depot_id)
                depot_transactions[depot_id] = await self.get_depot_transactions(
                    depot_id,
                    limit=depot_transaction_limit,
                    min_booking_date=depot_transaction_min_booking_date,
                )

        logger.info("get_all_data: complete")
        return {
            "accounts": accounts,
            "transactions": transactions,
            "depots": depots,
            "depot_positions": depot_positions,
            "depot_transactions": depot_transactions,
        }
