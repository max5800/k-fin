"""Persist Comdirect depot data into the portfolio tables.

Companion to `src.normalization.ingest` which handles bank-account
transactions. This module writes depot/position/instrument/
depot_transaction rows from a `ComdirectData` payload and captures a
daily portfolio snapshot per depot plus one aggregate row (depot_id
NULL) that drives the performance chart.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.connector.models import ComdirectData, DepotPosition, DepotTransaction
from src.core.db.models import (
    Depot,
    DepotTransaction as DepotTransactionRow,
    DepotTransactionType,
    Instrument,
    PortfolioSnapshot,
    Position,
)

logger = logging.getLogger(__name__)


def ingest_depots(session: Session, data: ComdirectData | dict[str, Any]) -> dict[str, int]:
    """Upsert depots, instruments, positions and depot transactions.

    Positions are snapshot-replaced per depot (stale holdings disappear).
    Instruments upsert on ISIN. Depot transactions are idempotent on
    `transaction_id`.
    """
    payload = _as_comdirect_data(data)

    depot_count = _upsert_depots(session, payload.depots)
    instrument_count = _collect_instruments(
        session, payload.depot_positions, payload.depot_transactions
    )
    position_count = _refresh_positions(session, payload.depot_positions)
    tx_count = _insert_depot_transactions(session, payload.depot_transactions)

    session.commit()
    stats = {
        "depots": depot_count,
        "instruments": instrument_count,
        "positions": position_count,
        "transactions": tx_count,
    }
    logger.info("depot ingest: %s", stats)
    return stats


def capture_snapshot(session: Session, as_of: date | None = None) -> int:
    """Write one `portfolio_snapshots` row per depot plus one aggregate row
    (depot_id NULL). Idempotent per day via UNIQUE(depot_id, snapshot_date).
    Returns the number of rows written (incl. updates).
    """
    as_of = as_of or date.today()
    depots = session.execute(select(Depot)).scalars().all()

    rows = 0
    total_value = Decimal("0")
    total_purchase = Decimal("0")

    for depot in depots:
        positions = (
            session.execute(select(Position).where(Position.depot_id == depot.depot_id))
            .scalars()
            .all()
        )
        depot_value = sum(
            (p.current_value or Decimal("0") for p in positions), Decimal("0")
        )
        depot_purchase = sum(
            (p.purchase_value or Decimal("0") for p in positions), Decimal("0")
        )
        total_value += depot_value
        total_purchase += depot_purchase
        _upsert_snapshot(
            session,
            depot_id=depot.depot_id,
            snapshot_date=as_of,
            total_value=depot_value,
            total_purchase=depot_purchase,
        )
        rows += 1

    # aggregate row (depot_id NULL)
    _upsert_snapshot(
        session,
        depot_id=None,
        snapshot_date=as_of,
        total_value=total_value,
        total_purchase=total_purchase,
    )
    rows += 1
    session.commit()
    return rows


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_comdirect_data(data: ComdirectData | dict[str, Any]) -> ComdirectData:
    if isinstance(data, ComdirectData):
        return data
    return ComdirectData.model_validate(data)


def _upsert_depots(session: Session, depots: Iterable[dict[str, Any]]) -> int:
    count = 0
    for raw in depots:
        depot_id = raw.get("depotId") or raw.get("depot_id")
        if not depot_id:
            continue
        client_id = raw.get("clientId") or raw.get("client_id")
        depot_type_raw = raw.get("depotType") or raw.get("depot_type")
        depot_type = (
            depot_type_raw.get("text")
            if isinstance(depot_type_raw, dict)
            else depot_type_raw
        )
        currency = raw.get("currency") or "EUR"

        stmt = pg_insert(Depot).values(
            depot_id=depot_id,
            client_id=client_id,
            depot_type=depot_type,
            currency=currency,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["depot_id"],
            set_={
                "client_id": stmt.excluded.client_id,
                "depot_type": stmt.excluded.depot_type,
                "currency": stmt.excluded.currency,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        session.execute(stmt)
        count += 1
    return count


def _collect_instruments(
    session: Session,
    positions_by_depot: dict[str, list[DepotPosition]],
    tx_by_depot: dict[str, list[DepotTransaction]],
) -> int:
    """Upsert every ISIN we've seen. Position data wins over transaction data
    because position payloads carry instrument_type."""
    seen: dict[str, dict[str, Any]] = {}
    for positions in positions_by_depot.values():
        for p in positions:
            if not p.isin:
                continue
            seen[p.isin] = {
                "isin": p.isin,
                "wkn": p.wkn or None,
                "name": p.name or "",
                "instrument_type": p.instrument_type or None,
                "currency": p.currency or "EUR",
            }
    for txs in tx_by_depot.values():
        for tx in txs:
            if not tx.isin or tx.isin in seen:
                continue
            seen[tx.isin] = {
                "isin": tx.isin,
                "wkn": tx.wkn or None,
                "name": tx.name or "",
                "instrument_type": None,
                "currency": tx.currency or "EUR",
            }

    for row in seen.values():
        stmt = pg_insert(Instrument).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["isin"],
            set_={
                "wkn": stmt.excluded.wkn,
                "name": stmt.excluded.name,
                "instrument_type": stmt.excluded.instrument_type,
                "currency": stmt.excluded.currency,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        session.execute(stmt)
    return len(seen)


def _refresh_positions(
    session: Session, positions_by_depot: dict[str, list[DepotPosition]]
) -> int:
    """Replace positions per depot. Stale rows are removed so the table
    reflects the latest depot snapshot rather than a growing union."""
    count = 0
    for depot_id, positions in positions_by_depot.items():
        session.execute(delete(Position).where(Position.depot_id == depot_id))
        for p in positions:
            if not p.isin:
                continue
            session.add(
                Position(
                    depot_id=depot_id,
                    isin=p.isin,
                    quantity=_d(p.quantity),
                    current_price=_d(p.current_price),
                    current_value=_d(p.current_value),
                    purchase_value=_d(p.purchase_value),
                    prev_day_price=_d(p.prev_day_price) if p.prev_day_price else None,
                    prev_day_value=_d(p.prev_day_value) if p.prev_day_value else None,
                    currency=p.currency or "EUR",
                )
            )
            count += 1
    return count


def _insert_depot_transactions(
    session: Session, tx_by_depot: dict[str, list[DepotTransaction]]
) -> int:
    count = 0
    for depot_id, txs in tx_by_depot.items():
        for tx in txs:
            if not tx.transaction_id or not tx.booking_date:
                continue
            try:
                booking = date.fromisoformat(tx.booking_date[:10])
            except ValueError:
                logger.warning("skipping depot tx with bad booking_date=%r", tx.booking_date)
                continue
            stmt = pg_insert(DepotTransactionRow).values(
                transaction_id=tx.transaction_id,
                depot_id=depot_id,
                isin=tx.isin or None,
                booking_date=booking,
                transaction_type=_coerce_tx_type(tx.transaction_type),
                quantity=_d(tx.quantity),
                price=_d(tx.price),
                amount=_d(tx.amount),
                currency=tx.currency or "EUR",
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["transaction_id"])
            result = session.execute(stmt)
            if result.rowcount:
                count += 1
    return count


def _upsert_snapshot(
    session: Session,
    *,
    depot_id: str | None,
    snapshot_date: date,
    total_value: Decimal,
    total_purchase: Decimal,
) -> None:
    # PostgreSQL treats NULL values as distinct in UNIQUE, so the aggregate
    # row (depot_id IS NULL) can't rely on ON CONFLICT. Handle it manually.
    if depot_id is None:
        existing = session.execute(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.depot_id.is_(None),
                PortfolioSnapshot.snapshot_date == snapshot_date,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.total_value = total_value
            existing.total_purchase_value = total_purchase
            existing.captured_at = datetime.now(timezone.utc)
        else:
            session.add(
                PortfolioSnapshot(
                    depot_id=None,
                    snapshot_date=snapshot_date,
                    total_value=total_value,
                    total_purchase_value=total_purchase,
                )
            )
        return

    stmt = pg_insert(PortfolioSnapshot).values(
        depot_id=depot_id,
        snapshot_date=snapshot_date,
        total_value=total_value,
        total_purchase_value=total_purchase,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["depot_id", "snapshot_date"],
        set_={
            "total_value": stmt.excluded.total_value,
            "total_purchase_value": stmt.excluded.total_purchase_value,
            "captured_at": datetime.now(timezone.utc),
        },
    )
    session.execute(stmt)


def _d(value: float | int | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _coerce_tx_type(raw: str) -> str:
    try:
        return DepotTransactionType(raw.upper()).value
    except (ValueError, AttributeError):
        return DepotTransactionType.OTHER.value
