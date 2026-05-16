"""PayPal Transaction Search API client — read-only (M16-P2b).

Auth is OAuth2 ``client_credentials`` — a pure machine-to-machine grant,
no user step (contrast Comdirect's pushTAN). The client exchanges the
app's client-id / secret for a short-lived Bearer token and reads
``GET /v1/reporting/transactions``.

Operational constraints baked into this module — see the methods below:

* **31-day window cap.** A single ``/v1/reporting/transactions`` call
  must span at most 31 days (``start_date`` … ``end_date``). Anything
  longer is rejected by PayPal, so :meth:`PayPalClient.get_transactions`
  fetches one ≤31-day window and :meth:`get_transactions_range` chunks a
  longer range (historical backfill) into consecutive sub-windows.
* **T+3h availability window.** A transaction is not guaranteed to be
  query-able until ~3 hours after it settles. An incremental sync that
  ends "now" simply misses the last few hours; the next sync re-fetches
  the overlap and the content-hash dedup absorbs it — no special-casing.
* **Rate limit.** The reporting API is rate-limited (HTTP 429). Calls
  are sequential and page-bounded; no retry/backoff loop here — a 429
  surfaces as a hard error and the sync is retried by the user later.
* **Idempotency.** ``transaction_info.transaction_id`` is PayPal's
  stable per-transaction identifier; it lands in the canonical
  ``external_id`` and the ``(source, external_id)`` scoping makes a
  re-fetched window a no-op at ingest time.

Sandbox vs. live is selected by ``settings.paypal_environment``.
"""

from __future__ import annotations

import base64
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

import httpx

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger("paypal")

_LIVE_BASE_URL = "https://api-m.paypal.com"
_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

# PayPal's hard cap on a single reporting-window span.
WINDOW_MAX_DAYS = 31
# `page_size` ceiling for /v1/reporting/transactions.
_PAGE_SIZE = 500
# Re-auth this many seconds before the token's stated expiry, so a
# long-running sync never races the expiry boundary mid-request.
_TOKEN_EXPIRY_SKEW_SECONDS = 60


class PayPalClient:
    """Thin async client over the PayPal Transaction Search API.

    Stateless between syncs except for an in-memory token cache. A new
    ``httpx.AsyncClient`` is opened per request (mirrors
    :class:`~src.external.comdirect_client.ComdirectClient`).
    """

    def __init__(self) -> None:
        self.client_id = settings.paypal_client_id
        self.client_secret = settings.paypal_client_secret
        self.environment = (settings.paypal_environment or "sandbox").strip().lower()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def base_url(self) -> str:
        """The API host — live only when ``paypal_environment == "live"``,
        sandbox for every other value (fail-safe to the non-live host)."""
        return _LIVE_BASE_URL if self.environment == "live" else _SANDBOX_BASE_URL

    # ------------------------------------------------------------------
    # Auth — OAuth2 client_credentials
    # ------------------------------------------------------------------

    async def _bearer_token(self) -> str:
        """Return a valid Bearer token, refreshing it if missing/expired."""
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        if response.status_code != 200:
            logger.error("PayPal OAuth failed: HTTP %s", response.status_code)
            raise RuntimeError(
                f"PayPal OAuth token request failed: HTTP {response.status_code}"
            )
        data = response.json()
        self._access_token = data["access_token"]
        # `expires_in` is seconds; keep a skew margin against clock drift.
        ttl = int(data.get("expires_in", 0)) - _TOKEN_EXPIRY_SKEW_SECONDS
        self._token_expires_at = time.monotonic() + max(ttl, 0)
        logger.info("PayPal OAuth OK (%s)", self.environment)
        return self._access_token

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    async def get_transactions(
        self, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Fetch every transaction in a single ≤31-day window.

        ``start`` / ``end`` are inclusive calendar dates; the request
        spans ``[start 00:00, end 23:59:59]``. Raises ``ValueError`` if
        the span exceeds :data:`WINDOW_MAX_DAYS` — the caller must chunk
        first (see :meth:`get_transactions_range`).

        Returns the raw ``transaction_details`` dicts, paginated until
        ``total_pages`` is exhausted.
        """
        if (end - start).days > WINDOW_MAX_DAYS:
            raise ValueError(
                f"PayPal window {start}..{end} exceeds the {WINDOW_MAX_DAYS}-day "
                "cap — chunk via get_transactions_range()"
            )

        token = await self._bearer_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        params_base = {
            "start_date": _iso_start(start),
            "end_date": _iso_end(end),
            "fields": "all",
            "page_size": _PAGE_SIZE,
        }

        details: list[dict[str, Any]] = []
        page = 1
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                response = await client.get(
                    f"{self.base_url}/v1/reporting/transactions",
                    params={**params_base, "page": page},
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json()
                details.extend(body.get("transaction_details") or [])
                total_pages = int(body.get("total_pages", 1) or 1)
                if page >= total_pages:
                    break
                page += 1

        logger.info(
            "PayPal: fetched %d transactions for %s..%s", len(details), start, end
        )
        return details

    async def get_transactions_range(
        self, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Fetch a range of any length, chunked into ≤31-day windows.

        Consecutive windows are half-open at the ingest level (the
        content-hash dedup makes a one-day overlap harmless), so the
        chunks are simply walked back-to-back. Used for historical
        backfill; an incremental sync calls :meth:`get_transactions`
        directly with a single sub-31-day window.
        """
        if start > end:
            raise ValueError(f"PayPal range start {start} is after end {end}")

        details: list[dict[str, Any]] = []
        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=WINDOW_MAX_DAYS), end)
            details.extend(await self.get_transactions(window_start, window_end))
            window_start = window_end + timedelta(days=1)
        return details


def _iso_start(day: date) -> str:
    """Start-of-day UTC timestamp in the ISO-8601 form PayPal expects."""
    return datetime.combine(day, dtime.min, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S-0000"
    )


def _iso_end(day: date) -> str:
    """End-of-day UTC timestamp in the ISO-8601 form PayPal expects."""
    return datetime.combine(day, dtime.max, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S-0000"
    )
