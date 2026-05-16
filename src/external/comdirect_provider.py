"""Comdirect adapter — the first :class:`BankProvider` implementation.

A thin wrapper over the existing :class:`~src.external.comdirect_client.ComdirectClient`
(M16-P2a). No rewrite: ``start_sync`` / ``complete_sync`` mirror the
client's ``begin_auth`` / ``complete_auth`` + ``get_all_data`` calls onto
the provider contract, and the canonical stream reuses the unchanged
``build_export`` → ``canonicalize`` path so every ``content_hash`` stays
byte-identical to the legacy file-import sync.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db.models import DataSource
from src.core.logging import get_logger
from src.exporter.json_export import build_export
from src.external.comdirect_client import ComdirectClient
from src.external.models import ComdirectAccount
from src.external.provider import (
    Account,
    BankProviderBase,
    CanonicalTransaction,
    DepotSupport,
    SyncChallenge,
    TanKind,
)
from src.normalization.canonicalize import canonicalize

logger = get_logger("comdirect-provider")

# Disk location for the CSV / JSON export artifacts — unchanged from the
# pre-P2a worker (``/data/exports`` is the mounted volume in the worker pod).
_EXPORT_DIR = Path("/data/exports")


class ComdirectProvider(BankProviderBase):
    """Comdirect REST API as a :class:`BankProvider`.

    Wraps :class:`ComdirectClient` — the client is the single source of
    truth for the OAuth2 / pushTAN flow; this adapter only maps it onto
    the contract. ``complete_sync`` caches the fetched payload on the
    instance so ``post_sync_hook`` can reuse it without a second
    round-trip to Comdirect.
    """

    display_name = "Comdirect"
    supports_depot = DepotSupport.FULL
    supports_watchlist = False
    supports_orders = False
    supports_pending_bookings = True
    tan_kind = TanKind.DECOUPLED_APP_PUSH

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # ``config`` is the optional SyncStartRequest body — fetch-window
        # overrides (account_transaction_limit, …). Missing keys fall back
        # to settings defaults in :meth:`_fetch_kwargs`.
        self._config: dict[str, Any] = config or {}
        self._client = ComdirectClient()
        self._session_identifier: str | None = None
        self._challenge_id: str | None = None
        # Cached between complete_sync() and post_sync_hook(): the raw
        # get_all_data() payload and its build_export() projection.
        self._raw_data: dict[str, Any] | None = None
        self._export_payload: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Contract — sync lifecycle
    # ------------------------------------------------------------------

    async def start_sync(self) -> SyncChallenge:
        """Begin auth (steps 1-3) and trigger the pushTAN challenge."""
        auth_state = await self._client.begin_auth()
        self._session_identifier = auth_state["session_identifier"]
        self._challenge_id = auth_state["challenge_id"]
        # Comdirect pushTAN is always decoupled photoTAN — the client does
        # not surface the TAN `typ` from begin_auth(), so the hint is fixed.
        return SyncChallenge(
            challenge_id=auth_state["challenge_id"],
            tan_kind=TanKind.DECOUPLED_APP_PUSH,
            display_hint="photoTAN",
        )

    async def complete_sync(
        self, challenge_response: str | None
    ) -> AsyncIterator[CanonicalTransaction]:
        """Complete auth (steps 4-6), fetch all data, stream canonical txns.

        ``challenge_response`` is unused — Comdirect's decoupled pushTAN
        is confirmed in the banking app out-of-band before this is called.

        The fetched payload runs through the unchanged ``build_export``
        projection so each yielded transaction is the exact dict shape
        the legacy ``ingest_json_export`` path canonicalizes — the
        ``content_hash`` is byte-identical.
        """
        if self._session_identifier is None or self._challenge_id is None:
            raise RuntimeError("complete_sync() called before start_sync()")

        ok = await self._client.complete_auth(
            self._session_identifier, self._challenge_id
        )
        if not ok:
            raise RuntimeError("Session activation failed")

        self._raw_data = await self._client.get_all_data(**self._fetch_kwargs())
        self._export_payload = build_export(self._raw_data)

        transactions_by_account = self._export_payload.get("transactions") or {}
        for _account_id, txs in transactions_by_account.items():
            for tx in txs:
                yield CanonicalTransaction(
                    canonical=canonicalize(tx),
                    raw_data=tx,
                    source=DataSource.COMDIRECT,
                )

    async def list_accounts(self) -> list[Account]:
        """Return the Comdirect accounts (cached from complete_sync if available)."""
        raw_accounts = (
            self._raw_data["accounts"]
            if self._raw_data is not None
            else await self._client.get_accounts()
        )
        accounts: list[Account] = []
        for raw in raw_accounts:
            model = ComdirectAccount.model_validate(raw)
            accounts.append(
                Account(
                    account_id=model.account_id,
                    iban=model.iban,
                    account_type=model.account_type,
                    balance=Decimal(str(model.balance)),
                    currency=model.currency,
                )
            )
        return accounts

    async def list_depot_positions(self) -> list[dict[str, Any]]:
        """Return current depot holdings, flattened across all depots."""
        if self._raw_data is None:
            return []
        positions: list[dict[str, Any]] = []
        for depot_positions in (self._raw_data.get("depot_positions") or {}).values():
            positions.extend(depot_positions)
        return positions

    async def post_sync_hook(self, engine: Engine) -> dict[str, Any] | None:
        """Comdirect-specific tail: CSV + JSON disk exports and depot ingest.

        Best-effort — every sub-step is independently guarded so a failure
        is logged and the next step still runs. The transaction ingest +
        normalization already committed before this hook. Only valid
        after :meth:`complete_sync` (relies on the data cached there).

        Returns the depot-ingest stats, or ``None`` if there is nothing
        to do (no prior ``complete_sync``).
        """
        if self._raw_data is None or self._export_payload is None:
            return None

        try:
            _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            self._write_csv_exports(self._raw_data, _EXPORT_DIR)
            self._write_json_export(self._export_payload, _EXPORT_DIR)
        except Exception:
            logger.exception(
                "Comdirect CSV/JSON export failed (transaction ingest already succeeded)"
            )

        depot_stats: dict[str, int] | None = None
        try:
            from src.normalization.depot_ingest import capture_snapshot, ingest_depots

            with Session(engine) as depot_session:
                depot_stats = ingest_depots(depot_session, self._raw_data)
                capture_snapshot(depot_session, date.today())
            logger.info("Depot ingest: %s", depot_stats)
        except Exception:
            logger.exception(
                "Depot ingest failed (transaction ingest still succeeded)"
            )

        return {"depots": depot_stats}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_kwargs(self) -> dict[str, Any]:
        """Merge the request-supplied fetch window over the settings defaults."""
        cfg = self._config
        return {
            "account_transaction_limit": cfg.get(
                "account_transaction_limit", settings.account_transaction_limit
            ),
            "account_transaction_min_booking_date": cfg.get(
                "account_transaction_min_booking_date",
                settings.account_transaction_min_booking_date,
            ),
            "depot_transaction_limit": cfg.get(
                "depot_transaction_limit", settings.depot_transaction_limit
            ),
            "depot_transaction_min_booking_date": cfg.get(
                "depot_transaction_min_booking_date",
                settings.depot_transaction_min_booking_date,
            ),
        }

    @staticmethod
    def _write_csv_exports(data: dict[str, Any], output_dir: Path) -> None:
        """Write per-account / per-depot / summary CSVs (relocated verbatim
        from the pre-P2a worker's ``internal_sync_confirm``)."""
        from scripts.export_csv import (
            export_account_to_csv,
            export_depot_positions_csv,
            export_depot_transactions_csv,
            export_summary_csv,
        )

        for acc in data["accounts"]:
            account_inner = acc.get("account") or acc
            account_id = account_inner.get("accountId")
            iban = account_inner.get("iban", account_id)
            if not account_id:
                continue
            balance_obj = acc.get("balance") or {}
            acc_type_obj = account_inner.get("accountType") or {}
            acc_type_text = (
                acc_type_obj.get("text", "Girokonto")
                if isinstance(acc_type_obj, dict)
                else str(acc_type_obj)
            )
            balance_info = {
                "value": (
                    balance_obj.get("value", "0")
                    if isinstance(balance_obj, dict)
                    else str(balance_obj)
                ),
                "unit": (
                    balance_obj.get("unit", "EUR")
                    if isinstance(balance_obj, dict)
                    else "EUR"
                ),
                "account_type": acc_type_text,
            }
            txs = data["transactions"].get(account_id, [])
            export_account_to_csv(account_id, iban, balance_info, txs, output_dir)

        all_positions: list[dict] = []
        for depot in data["depots"]:
            depot_id = depot.get("depotId")
            if not depot_id:
                continue
            positions = data["depot_positions"].get(depot_id, [])
            all_positions.extend(positions)
            export_depot_positions_csv(depot_id, positions, output_dir)
            depot_txs = data["depot_transactions"].get(depot_id, [])
            export_depot_transactions_csv(depot_id, depot_txs, output_dir)

        export_summary_csv(data["accounts"], all_positions, output_dir)

    @staticmethod
    def _write_json_export(payload: dict[str, Any], output_dir: Path) -> None:
        """Write the model-based JSON export artifact for the day."""
        json_path = output_dir / f"comdirect_export_{date.today().isoformat()}.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
