"""Provider contract — the bank-neutral interface every live-API sync source implements.

M16-P2a generalizes the previously Comdirect-hardwired sync path into a
``BankProvider`` contract. The current registered implementations are:

* Comdirect (:mod:`src.external.comdirect_provider`) — full live-API sync
  with decoupled-push photoTAN.
* Santander credit card (:mod:`src.external.santander_provider`) — live
  MySantander scraping session, gated behind ``ENDPOINTS_VERIFIED``.

**File-import sources are NOT ``BankProvider`` implementations.**  PayPal
(CSV import via :mod:`src.normalization.paypal_csv`) and the Santander PDF
statement importer (:mod:`src.normalization.santander_pdf`) feed into the
same downstream canonical layer but bypass this contract entirely — they
have no ``start_sync`` / ``complete_sync`` lifecycle and are not in the
``PROVIDERS`` registry (:mod:`src.external.providers`).

The capability flags are deliberately cut so a future FinTS-Generic
provider (DKB / ING / Sparkasse via ``python-fints``) docks unchanged:
``supports_depot=PARTIAL`` (FinTS exposes holdings but no performance),
``supports_watchlist=False``, ``supports_orders=False``,
``tan_kind=DECOUPLED_APP_PUSH``. Adding a live-API source is exactly one
registry line plus one provider class implementing the three required
methods (``start_sync``, ``complete_sync``, ``list_accounts``).
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Protocol, runtime_checkable

from sqlalchemy.engine import Engine

from src.core.db.models import DataSource


class DepotSupport(str, enum.Enum):
    """How much depot / brokerage data a provider can return."""

    NONE = "none"
    PARTIAL = "partial"  # holdings only — no performance / orders (FinTS)
    FULL = "full"  # holdings + transactions + performance (Comdirect)


class TanKind(str, enum.Enum):
    """The strong-auth mechanism a provider's sync requires.

    Drives the UI TAN-modal copy *and* the background-sync policy (see
    the TAN-in-the-loop rule in ``00_PROJEKTPLAN_KANONISCH.md``): only
    ``NONE_OAUTH_M2M`` may run unattended; every other kind is strictly
    user-triggered, no silent background sync.
    """

    NONE_OAUTH_M2M = "none_oauth_m2m"  # machine-to-machine OAuth, no user step (reserved for future m2m providers)
    DECOUPLED_APP_PUSH = "decoupled_app_push"  # push to a banking app (Comdirect photoTAN, FinTS)
    SMS = "sms"
    EMAIL_OTP = "email_otp"
    SCRAPING_SESSION = "scraping_session"  # interactive web-session login (Santander-CC)


@dataclass(frozen=True, slots=True)
class SyncChallenge:
    """A pending strong-auth challenge returned by ``start_sync()``.

    ``start_sync()`` returns ``None`` instead of a ``SyncChallenge`` when
    the provider needs no user interaction (``tan_kind=NONE_OAUTH_M2M``).
    """

    challenge_id: str
    tan_kind: TanKind
    display_hint: str | None = None  # e.g. "photoTAN" — shown in the UI modal


@dataclass(frozen=True, slots=True)
class Account:
    """A bank account in provider-neutral shape."""

    account_id: str
    iban: str
    account_type: str
    balance: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class CanonicalTransaction:
    """One transaction on its way from a provider into ``raw_transactions``.

    A pure transport envelope — it carries exactly what the ingest layer
    needs and nothing else:

    * ``canonical`` — the output of
      :func:`src.normalization.canonicalize.canonicalize` (the ten
      ``CANONICAL_FIELDS_FOR_HASH``). ``content_hash`` is computed from
      this dict unchanged, so provider-sourced rows hash byte-identically
      to the legacy file-import path.
    * ``raw_data`` — the original provider payload, stored verbatim in
      ``raw_transactions.raw_data`` as the audit blob.
    * ``source`` — provenance; selects the hashing rule and fills the
      ``source`` column.
    """

    canonical: dict[str, Any]
    raw_data: dict[str, Any]
    source: DataSource


@runtime_checkable
class BankProvider(Protocol):
    """The contract every data source implements.

    Lifecycle: ``start_sync()`` opens a session and optionally returns a
    challenge the user must resolve; ``complete_sync()`` then finishes
    auth and streams every transaction as a ``CanonicalTransaction``.
    ``list_accounts()`` / ``list_depot_positions()`` expose reference
    data; ``post_sync_hook()`` runs source-specific tail work (Comdirect:
    CSV export + depot ingest).

    Concrete providers should inherit :class:`BankProviderBase` for the
    no-op defaults of the two optional methods.
    """

    #: Human-readable name shown in the UI TAN modal (e.g. "Comdirect").
    display_name: ClassVar[str]
    #: Depot / brokerage capability — see :class:`DepotSupport`.
    supports_depot: ClassVar[DepotSupport]
    supports_watchlist: ClassVar[bool]
    supports_orders: ClassVar[bool]
    supports_pending_bookings: ClassVar[bool]
    #: Strong-auth mechanism — drives modal copy and background-sync policy.
    tan_kind: ClassVar[TanKind]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Construct the provider.

        ``config`` carries optional per-sync overrides from the request
        body (e.g. Comdirect's fetch-window limits). The registry
        constructs every provider uniformly as ``provider_cls(config=...)``.
        """
        ...

    async def start_sync(self) -> SyncChallenge | None:
        """Open a sync session.

        Returns a :class:`SyncChallenge` the user must resolve out-of-band,
        or ``None`` when the provider needs no user interaction.
        """
        ...

    def complete_sync(
        self, challenge_response: str | None
    ) -> AsyncIterator[CanonicalTransaction]:
        """Finish auth and stream every transaction as a ``CanonicalTransaction``.

        ``challenge_response`` carries the OTP for ``SMS`` / ``EMAIL_OTP``
        providers; for decoupled-push and m2m it is unused (the TAN was
        confirmed out-of-band) and may be ``None``.

        This is an async generator — call it, then iterate the result.
        """
        ...

    async def list_accounts(self) -> list[Account]:
        """Return the provider's accounts."""
        ...

    async def list_depot_positions(self) -> list[dict[str, Any]]:
        """Return current depot holdings.

        Empty for providers with ``supports_depot=NONE`` — see
        :class:`BankProviderBase` for the inherited no-op default.
        """
        ...

    async def post_sync_hook(self, engine: Engine) -> dict[str, Any] | None:
        """Run source-specific work after the canonical stream is ingested.

        Comdirect uses it for CSV export + depot ingest. Default is a
        no-op (see :class:`BankProviderBase`). Only valid after
        ``complete_sync()`` has run on the same instance.
        """
        ...


class BankProviderBase:
    """Mixin supplying no-op defaults for the optional contract methods.

    A :class:`Protocol` cannot carry implementations, so concrete
    providers inherit this and override only what they actually support.
    A provider with ``supports_depot=NONE`` and no tail work (e.g.
    Santander-CC) needs neither override.
    """

    async def list_depot_positions(self) -> list[dict[str, Any]]:
        return []

    async def post_sync_hook(self, engine: Engine) -> dict[str, Any] | None:
        return None
