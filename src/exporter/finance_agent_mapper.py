"""Map raw Comdirect get_all_data() output to Finance Agent format."""

import logging
from datetime import datetime, timezone

from src.connector.models import (
    ComdirectAccount,
    ComdirectData,
    ComdirectTransaction,
    DepotPosition,
    DepotTransaction,
)

logger = logging.getLogger("sync.exporter")

# Account type key → canonical name
_ACCOUNT_TYPE_MAP = {
    "CURRENT_ACCOUNT": "girokonto",
    "GIRO": "girokonto",
    "CA": "girokonto",  # Comdirect mapped type
    "SAVINGS_ACCOUNT": "tagesgeld",
    "TAGESGELD": "tagesgeld",
    "SA": "tagesgeld",  # Comdirect mapped type
    "CLEARING_ACCOUNT": "verrechnungskonto",
    "DEPOT_VERRECHNUNGSKONTO": "verrechnungskonto",
    "DAS": "depot",  # Comdirect mapped type
}


def _account_canonical_name(account: ComdirectAccount) -> str:
    """Return canonical name for an account."""
    raw_type = account.account_type
    canonical = _ACCOUNT_TYPE_MAP.get(raw_type.upper() if raw_type else "")
    if canonical:
        return canonical
    fallback = raw_type.lower().replace(" ", "_") if raw_type else "unknown"
    logger.warning("Unknown account type %r — placing under key %r", raw_type, fallback)
    return fallback


def _map_transaction(tx: ComdirectTransaction) -> dict:
    if tx.amount < 0:
        counterpart_name = tx.creditor_name
        counterpart_iban = tx.creditor_iban
    else:
        counterpart_name = tx.debtor_name
        counterpart_iban = tx.debtor_iban

    return {
        "date": tx.booking_date,
        "booking_date": tx.booking_date,
        "value_date": tx.value_date,
        "type": tx.type_text,
        "text": tx.remittance_info,
        "amount": tx.amount,
        "currency": tx.currency,
        "counterpart_name": counterpart_name,
        "counterpart_iban": counterpart_iban,
        "transaction_id": tx.transaction_id,
    }


def _compute_summary(transactions: list[dict]) -> dict:
    total_in = sum(t["amount"] for t in transactions if t["amount"] > 0)
    total_out = abs(sum(t["amount"] for t in transactions if t["amount"] < 0))
    return {
        "total_in": round(total_in, 2),
        "total_out": round(total_out, 2),
        "net": round(total_in - total_out, 2),
        "count": len(transactions),
    }


def _map_depot_position(pos: DepotPosition) -> dict:
    return {
        "isin": pos.isin,
        "wkn": pos.wkn,
        "name": pos.name,
        "quantity": pos.quantity,
        "current_price": pos.current_price,
        "current_value": pos.current_value,
        "purchase_value": pos.purchase_value,
        "currency": pos.currency,
        "gains": pos.gains,
        "gains_percent": pos.gains_percent,
    }


def _map_depot_transaction(tx: DepotTransaction) -> dict:
    return {
        "date": tx.booking_date,
        "isin": tx.isin,
        "wkn": tx.wkn,
        "name": tx.name,
        "transaction_type": tx.transaction_type,
        "quantity": tx.quantity,
        "price": tx.price,
        "amount": tx.amount,
        "currency": tx.currency,
        "transaction_id": tx.transaction_id,
    }


def _depot_summary(positions: list[dict]) -> dict:
    total_value = sum(p["current_value"] for p in positions)
    total_purchase = sum(p["purchase_value"] for p in positions)
    total_gains = round(total_value - total_purchase, 2)
    total_gains_pct = round((total_gains / total_purchase * 100) if total_purchase else 0, 4)
    return {
        "total_value": round(total_value, 2),
        "total_purchase_value": round(total_purchase, 2),
        "total_gains": total_gains,
        "total_gains_percent": total_gains_pct,
        "position_count": len(positions),
    }


def map_to_finance_agent(raw: dict) -> dict:
    """Transform raw get_all_data() output into the Finance Agent format.

    Parses the raw dict into ComdirectData first, then maps
    the validated Pydantic models to the agent output shape.
    """
    data = ComdirectData.model_validate(raw)

    result: dict = {}
    total_tx_count = 0

    # --- Bank accounts ---
    for account in data.accounts:
        account_id = account.account_id
        canonical = _account_canonical_name(account)

        raw_txs = data.transactions.get(account_id) or []
        mapped_txs = [_map_transaction(tx) for tx in raw_txs]
        total_tx_count += len(mapped_txs)

        entry = {
            "account_id": account_id,
            "transactions": mapped_txs,
            "summary": _compute_summary(mapped_txs),
        }

        if canonical in result:
            # Merge if same type appears twice (edge case)
            result[canonical]["transactions"].extend(entry["transactions"])
            result[canonical]["summary"] = _compute_summary(result[canonical]["transactions"])
        else:
            result[canonical] = entry

    # --- Depot ---
    if data.depots:
        depot = data.depots[0]
        depot_id = depot.get("depotId") or ""
        positions = [_map_depot_position(p) for p in (data.depot_positions.get(depot_id) or [])]
        dep_txs = [
            _map_depot_transaction(tx) for tx in (data.depot_transactions.get(depot_id) or [])
        ]
        total_tx_count += len(dep_txs)

        result["depot"] = {
            "depot_id": depot_id,
            "positions": positions,
            "transactions": dep_txs,
            "summary": _depot_summary(positions),
        }
    else:
        result["depot"] = {
            "depot_id": "",
            "positions": [],
            "transactions": [],
            "summary": _depot_summary([]),
        }

    result["meta"] = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account_count": len(data.accounts),
        "total_transaction_count": total_tx_count,
    }

    return result
