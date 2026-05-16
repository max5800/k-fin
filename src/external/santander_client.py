"""Santander Consumer Bank credit-card client — read-only (M16-P2c).

Anbindung der **Santander 1plus Visa Card** (Santander Consumer Bank AG).
Die Strategie-Entscheidung steht in ``docs/integrations/santander.md``; dieses
Modul implementiert sie.

**Strikt read-only.** Es gibt keinen einzigen Mutation-Pfad — nur Login,
Session-Status und Umsatz-Abruf. Niemals wird eine schreibende Operation
gegen Santander ausgeführt.

Auth-Flow (``tan_kind = SCRAPING_SESSION`` — siehe Spike §3)::

    1. begin_login()          — Username/Passwort-POST, löst 2FA aus
    2. <User bestätigt out-of-band: App-Push bzw. mobileTAN>
    3. is_session_elevated()  — prüft, ob die Session bestätigt ist
    4. get_cards() / get_card_transactions() — Umsatz-Abruf

**Session-Lifetime & Re-Auth** (Annahmen — per Live-Capture zu schärfen, Spike §5):
Die MySantander-Session ist Cookie-basiert und vermutlich kurzlebig
(~10–15 min Inaktivität). Es wird **keine** Session über Sync-Läufe hinweg
persistiert — jeder Sync ist ein voller ``begin_login`` → 2FA →
``is_session_elevated`` Zyklus. Der optionale ``settings.santander_device_id``
kann ein bestätigtes Gerät referenzieren und so 2FA-Friktion reduzieren.

**Endpoint-Verifikation:** Host und Pfade unten sind der *angenommene*
Contract aus Spike §4. Sie sind **noch nicht** gegen die Live-Umgebung
verifiziert. Solange ``ENDPOINTS_VERIFIED`` ``False`` ist, schlägt ein echter
Sync mit einem klaren :class:`SantanderError` fehl; Tests injizieren einen
``httpx``-Mock-Transport und umgehen das Gate bewusst. Vor dem ersten echten
Live-Sync: Capture-Checkliste (Spike §4) abarbeiten, Host/Pfade/Feldnamen
eintragen, ``ENDPOINTS_VERIFIED = True`` setzen.

**Defensive Drift-Behandlung:** Jede unerwartete Antwortstruktur löst einen
klaren :class:`SantanderParseError` / :class:`SantanderAuthError` aus, statt
stillschweigend leere oder falsche Daten zu liefern — das gibt einem späteren
AI-Agenten bei Struktur-Drift einen eindeutigen Ankerpunkt.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from src.core.config import settings
from src.core.logging import get_logger
from src.external.santander_models import SantanderCard, SantanderTransaction

logger = get_logger("santander")

# --- Assumed API contract (Spike §4) — NOT YET LIVE-VERIFIED. ---------------
# `.invalid` per RFC 2606: a real sync against this host fails fast and
# unmistakably until the capture checklist has run.
_BASE_URL = "https://mysantander-api.invalid"
_PATHS = {
    "login": "/api/auth/login",
    "session": "/api/auth/session",
    "cards": "/api/credit-cards",
    "transactions": "/api/credit-cards/{card_id}/transactions",
}
#: Flip to ``True`` only after the Spike §4 capture checklist verified
#: host, paths and field names against the live MySantander environment.
ENDPOINTS_VERIFIED = False

_TIMEOUT = httpx.Timeout(30.0)


class SantanderError(RuntimeError):
    """Base class for every Santander connector failure."""


class SantanderAuthError(SantanderError):
    """Login failed, or the session was not elevated after the 2FA step."""


class SantanderParseError(SantanderError):
    """An upstream response did not match the expected structure (drift)."""


class SantanderClient:
    """Thin async client over the MySantander credit-card surface.

    Stateful across one sync: :meth:`begin_login` opens a cookie-bearing
    session that :meth:`is_session_elevated` and the read methods reuse.
    The same instance is held by :class:`~src.external.santander_provider.SantanderProvider`
    between ``start_sync`` and ``complete_sync`` so the session survives the
    out-of-band TAN step. Call :meth:`close` when done.

    ``transport`` is for tests only — injecting an :class:`httpx.MockTransport`
    feeds synthetic responses and bypasses the :data:`ENDPOINTS_VERIFIED` gate.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self.username = settings.santander_username
        self.password = settings.santander_password
        self.device_id = settings.santander_device_id or None
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._challenge_id: str | None = None

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    def _require_live_endpoints(self) -> None:
        """Guard a real network call until the endpoint contract is verified."""
        if self._transport is None and not ENDPOINTS_VERIFIED:
            raise SantanderError(
                "Santander endpoints are not yet live-verified — run the "
                "capture checklist in docs/integrations/santander.md §4 and "
                "set ENDPOINTS_VERIFIED=True before a real sync."
            )

    def _client(self) -> httpx.AsyncClient:
        """Lazily open the cookie-bearing async client (one per sync)."""
        if self._http is None:
            kwargs: dict[str, Any] = {"base_url": _BASE_URL, "timeout": _TIMEOUT}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    async def close(self) -> None:
        """Close the underlying HTTP session. Safe to call more than once."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Auth — SCRAPING_SESSION (Spike §3)
    # ------------------------------------------------------------------

    async def begin_login(self) -> dict[str, Any]:
        """Submit username/password and trigger the 2FA challenge.

        Returns an auth-state dict with ``challenge_id`` — the user then
        confirms out-of-band (Santander app push / mobileTAN). Raises
        :class:`SantanderAuthError` on a rejected credential.
        """
        self._require_live_endpoints()
        if not self.username or not self.password:
            raise SantanderAuthError(
                "Santander credentials missing — set SANTANDER_USERNAME / "
                "SANTANDER_PASSWORD."
            )
        payload: dict[str, Any] = {
            "username": self.username,
            "password": self.password,
        }
        if self.device_id:
            payload["deviceId"] = self.device_id

        resp = await self._client().post(_PATHS["login"], json=payload)
        if resp.status_code in (401, 403):
            raise SantanderAuthError("Santander login rejected (HTTP %s)" % resp.status_code)
        if resp.status_code >= 400:
            raise SantanderError(f"Santander login failed: HTTP {resp.status_code}")

        body = _json(resp)
        self._challenge_id = body.get("challengeId") or body.get("challenge_id")
        logger.info("Santander login OK — 2FA challenge issued")
        return {
            "challenge_id": self._challenge_id,
            # Some flows (a remembered device) skip 2FA entirely.
            "challenge_required": bool(self._challenge_id),
        }

    async def is_session_elevated(self) -> bool:
        """Return whether the out-of-band 2FA confirmation has elevated the session."""
        self._require_live_endpoints()
        resp = await self._client().get(_PATHS["session"])
        if resp.status_code >= 400:
            raise SantanderAuthError(
                f"Santander session check failed: HTTP {resp.status_code}"
            )
        body = _json(resp)
        return bool(body.get("elevated") or body.get("status") == "elevated")

    # ------------------------------------------------------------------
    # Read-only data access
    # ------------------------------------------------------------------

    async def get_cards(self) -> list[SantanderCard]:
        """Return the user's credit cards. Requires an elevated session."""
        self._require_live_endpoints()
        resp = await self._client().get(_PATHS["cards"])
        if resp.status_code == 401:
            raise SantanderAuthError("Santander session not elevated for card list")
        if resp.status_code >= 400:
            raise SantanderError(f"Santander card list failed: HTTP {resp.status_code}")
        return _parse_list(_json(resp), "cards", SantanderCard)

    async def get_card_transactions(
        self, card_id: str
    ) -> list[SantanderTransaction]:
        """Return every transaction for one credit card.

        Strictly read-only. Defensive parsing — a structurally unexpected
        response raises :class:`SantanderParseError`.
        """
        self._require_live_endpoints()
        path = _PATHS["transactions"].format(card_id=card_id)
        resp = await self._client().get(path)
        if resp.status_code == 401:
            raise SantanderAuthError("Santander session not elevated for transactions")
        if resp.status_code >= 400:
            raise SantanderError(
                f"Santander transactions failed: HTTP {resp.status_code}"
            )
        return _parse_list(_json(resp), "transactions", SantanderTransaction)


# ---------------------------------------------------------------------------
# Defensive parsing helpers
# ---------------------------------------------------------------------------


def _json(resp: httpx.Response) -> dict[str, Any]:
    """Decode a JSON body, turning a non-JSON response into a clear error."""
    try:
        body = resp.json()
    except ValueError as exc:
        raise SantanderParseError(
            "Santander returned a non-JSON response — endpoint structure "
            "may have drifted (see docs/integrations/santander.md)."
        ) from exc
    if not isinstance(body, dict):
        raise SantanderParseError(
            f"Santander response was a {type(body).__name__}, expected an object."
        )
    return body


def _parse_list(body: dict[str, Any], key: str, model: type) -> list:
    """Validate ``body[key]`` (a list) into ``model`` instances.

    A missing key or a non-list value is treated as drift — :class:`SantanderParseError`
    — rather than silently yielding an empty result.
    """
    items = body.get(key)
    if items is None:
        raise SantanderParseError(
            f"Santander response is missing the {key!r} array — "
            "endpoint structure may have drifted."
        )
    if not isinstance(items, list):
        raise SantanderParseError(
            f"Santander {key!r} field is a {type(items).__name__}, expected a list."
        )
    parsed = []
    for raw in items:
        try:
            parsed.append(model.model_validate(raw))
        except ValidationError as exc:
            raise SantanderParseError(
                f"Santander {key!r} item failed validation: {exc}"
            ) from exc
    return parsed
