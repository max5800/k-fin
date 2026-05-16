"""Santander credit-card adapter — a :class:`BankProvider` over MySantander.

M16-P2c. The third provider on the P2a contract. Santander Consumer Bank's
credit-card surface needs an interactive strong-auth step, so
``tan_kind`` is :attr:`TanKind.SCRAPING_SESSION` and the sync is two-phase:

* :meth:`start_sync` logs in and triggers the 2FA challenge, returning a
  :class:`SyncChallenge`. The user confirms out-of-band (Santander app push /
  mobileTAN) — **no OTP is typed into k-fin**, which keeps the worker's
  ``/complete`` route body-less and unchanged.
* :meth:`complete_sync` verifies the session is elevated and streams every
  credit-card transaction as a :class:`CanonicalTransaction`.

A remembered device (``settings.santander_device_id``) can skip 2FA — then
:meth:`start_sync` returns ``None`` and the worker runs the sync inline.

Santander follows the TAN-in-the-loop rule (Spike §3): ``SCRAPING_SESSION`` is
strictly user-triggered — never an unattended background sync. No depot, no
watchlist, no orders; pending (not-yet-billed) authorisations *are* surfaced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from src.core.db.models import DataSource
from src.core.logging import get_logger
from src.external.provider import (
    Account,
    BankProviderBase,
    CanonicalTransaction,
    DepotSupport,
    SyncChallenge,
    TanKind,
)
from src.external.santander_client import SantanderAuthError, SantanderClient
from src.normalization.santander_canonicalize import santander_canonicalize

logger = get_logger("santander-provider")


class SantanderProvider(BankProviderBase):
    """Santander Consumer Bank credit card as a :class:`BankProvider`."""

    display_name = "Santander Kreditkarte"
    supports_depot = DepotSupport.NONE
    supports_watchlist = False
    supports_orders = False
    # Credit cards routinely carry pending authorisations — the connector
    # surfaces them (Spike §9).
    supports_pending_bookings = True
    tan_kind = TanKind.SCRAPING_SESSION

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config: dict[str, Any] = config or {}
        self._client = SantanderClient()
        self._challenge_pending = False

    async def start_sync(self) -> SyncChallenge | None:
        """Log in and trigger the 2FA challenge.

        Returns a :class:`SyncChallenge` for the normal flow, or ``None``
        when a remembered device let the login skip 2FA outright.
        """
        auth_state = await self._client.begin_login()
        if not auth_state.get("challenge_required"):
            logger.info("Santander: device remembered — 2FA skipped")
            self._challenge_pending = False
            return None
        self._challenge_pending = True
        return SyncChallenge(
            challenge_id=auth_state.get("challenge_id") or "santander-2fa",
            tan_kind=TanKind.SCRAPING_SESSION,
            display_hint="Santander App / mobileTAN",
        )

    async def complete_sync(
        self, challenge_response: str | None
    ) -> AsyncIterator[CanonicalTransaction]:
        """Verify the session, then stream every credit-card transaction.

        ``challenge_response`` is unused — the 2FA is confirmed out-of-band
        before this runs (decoupled, like Comdirect's pushTAN).
        """
        try:
            if self._challenge_pending and not await self._client.is_session_elevated():
                raise SantanderAuthError(
                    "Santander session not elevated — 2FA was not confirmed."
                )

            cards = await self._client.get_cards()
            logger.info("Santander: %d credit card(s)", len(cards))
            for card in cards:
                transactions = await self._client.get_card_transactions(card.card_id)
                logger.info(
                    "Santander: %d transaction(s) on card ••%s",
                    len(transactions),
                    card.last4,
                )
                for tx in transactions:
                    # `card_last4` may only be known at the card level —
                    # backfill it so the canonical id stays deterministic.
                    if not tx.card_last4:
                        tx.card_last4 = card.last4
                    raw = tx.model_dump(mode="json")
                    yield CanonicalTransaction(
                        canonical=santander_canonicalize(raw),
                        raw_data=raw,
                        source=DataSource.SANTANDER_CC,
                    )
        finally:
            await self._client.close()

    async def list_accounts(self) -> list[Account]:
        """A credit card is not modelled as a bank :class:`Account` (no IBAN)."""
        return []
