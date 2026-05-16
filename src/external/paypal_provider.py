"""PayPal adapter — a :class:`BankProvider` over the Transaction Search API.

M16-P2b. The second provider to dock onto the P2a contract. PayPal needs
no strong-auth user step: its ``client_credentials`` OAuth is pure
machine-to-machine, so ``tan_kind`` is :attr:`TanKind.NONE_OAUTH_M2M` and
:meth:`start_sync` returns ``None`` — the worker then runs the whole sync
inline (see ``internal_sync_start`` in ``main.py``).

PayPal has no depot, no watchlist, no orders and no pending-booking
concept, so every capability flag but ``tan_kind`` is the "off" value and
the no-op ``list_depot_positions`` / ``post_sync_hook`` from
:class:`BankProviderBase` are inherited unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

from src.core.db.models import DataSource
from src.core.logging import get_logger
from src.external.paypal_client import PayPalClient
from src.external.provider import (
    Account,
    BankProviderBase,
    CanonicalTransaction,
    DepotSupport,
    SyncChallenge,
    TanKind,
)
from src.normalization.paypal_canonicalize import paypal_canonicalize

logger = get_logger("paypal-provider")

# Incremental-sync look-back. One PayPal window (≤31 days) covers a
# month of activity; a re-sync overlaps the previous one and the
# content-hash dedup absorbs the repeats. A longer history is a backfill
# concern — pass ``window_days`` in the sync config to widen it (the
# client chunks anything over 31 days into consecutive windows).
DEFAULT_SYNC_WINDOW_DAYS = 31


class PayPalProvider(BankProviderBase):
    """PayPal Transaction Search API as a :class:`BankProvider`."""

    display_name = "PayPal"
    supports_depot = DepotSupport.NONE
    supports_watchlist = False
    supports_orders = False
    supports_pending_bookings = False
    tan_kind = TanKind.NONE_OAUTH_M2M

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # ``config`` is the optional sync-request body. Only ``window_days``
        # is honoured (incremental look-back); everything else is ignored.
        self._config: dict[str, Any] = config or {}
        self._client = PayPalClient()

    async def start_sync(self) -> SyncChallenge | None:
        """No user interaction — m2m OAuth needs no challenge."""
        return None

    async def complete_sync(
        self, challenge_response: str | None
    ) -> AsyncIterator[CanonicalTransaction]:
        """Authenticate, fetch the look-back window, stream canonical txns.

        ``challenge_response`` is unused — PayPal has no TAN step.
        """
        end = date.today()
        window_days = int(
            self._config.get("window_days", DEFAULT_SYNC_WINDOW_DAYS)
        )
        start = end - timedelta(days=max(window_days, 1))

        raw_transactions = await self._client.get_transactions_range(start, end)
        logger.info(
            "PayPal sync: %d transactions over %s..%s",
            len(raw_transactions),
            start,
            end,
        )
        for raw in raw_transactions:
            yield CanonicalTransaction(
                canonical=paypal_canonicalize(raw),
                raw_data=raw,
                source=DataSource.PAYPAL,
            )

    async def list_accounts(self) -> list[Account]:
        """PayPal exposes a single balance account with no IBAN — it is
        not modelled as a bank :class:`Account`, so this is empty."""
        return []
