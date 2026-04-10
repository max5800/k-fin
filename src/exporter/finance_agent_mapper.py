"""Map raw Comdirect get_all_data() output to Finance Agent format."""

import logging
from datetime import datetime, timezone

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

# Depot transaction type normalisation
_DEPOT_TYPE_MAP = {
    "BUY": "BUY",
    "IN": "BUY",
    "KAUF": "BUY",
    "SELL": "SELL",
    "OUT": "SELL",
    "VERKAUF": "SELL",
    "DIVIDEND": "DIVIDEND",
    "ERTRAG": "DIVIDEND",
}


def _get_val(obj, fallback=0.0):
    if obj is None:
        return fallback
    if isinstance(obj, dict):
        return float(obj.get("value") or fallback)
    return float(obj)


def _get_unit(obj, fallback=""):
    if obj is None:
        return fallback
    if isinstance(obj, dict):
        return obj.get("unit", fallback)
    return ""


def _account_canonical_name(account: dict) -> str:
    """Return canonical name for an account dict or a snake_case fallback."""
    raw_type = (
        account.get("accountType", {}).get("key")
        or account.get("account", {}).get("accountType", {}).get("key")
        or account.get("account", {}).get("accountType")
        or account.get("account_type")
        or ""
    )
    canonical = _ACCOUNT_TYPE_MAP.get(raw_type.upper() if raw_type else "")
    if canonical:
        return canonical
    fallback = raw_type.lower().replace(" ", "_") if raw_type else "unknown"
    logger.warning("Unknown account type %r — placing under key %r", raw_type, fallback)
    return fallback


def _map_transaction(tx: dict) -> dict:
    tx_value = tx.get("transactionValue") or tx.get("amount") or {}
    amount = _get_val(tx_value)
    currency = _get_unit(tx_value) or tx.get("currency") or ""

    creditor = tx.get("creditor") or {}
    debtor = tx.get("debtor") or {}
    if amount < 0:
        counterpart = creditor
    else:
        counterpart = debtor

    return {
        "date": tx.get("bookingDate") or tx.get("booking_date") or "",
        "booking_date": tx.get("bookingDate") or tx.get("booking_date") or "",
        "value_date": tx.get("valutaDate") or "",
        "type": tx.get("typeText") or "",
        "text": tx.get("remittanceInfo") or "",
        "amount": amount,
        "currency": currency,
        "counterpart_name": counterpart.get("holderName") or "",
        "counterpart_iban": counterpart.get("iban") or "",
        "transaction_id": tx.get("transactionId") or "",
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


def _map_depot_position(pos: dict) -> dict:
    current_value = _get_val(
        pos.get("currentValue") or pos.get("kurswert") or pos.get("current_value")
    )
    purchase_value = _get_val(
        pos.get("purchaseValue")
        or pos.get("einstandswert")
        or pos.get("purchase_value")
    )
    gains = round(current_value - purchase_value, 2)
    gains_percent = round((gains / purchase_value * 100) if purchase_value else 0, 4)

    instrument = pos.get("instrument") or {}
    price_value = _get_val(
        pos.get("currentPrice") or pos.get("kurs") or pos.get("current_price")
    )

    return {
        "isin": instrument.get("isin") or pos.get("isin") or "",
        "wkn": instrument.get("wkn") or pos.get("wkn") or "",
        "name": instrument.get("name") or pos.get("name") or "",
        "quantity": _get_val(pos.get("quantity") or pos.get("stueckzahl")),
        "current_price": float(price_value),
        "current_value": current_value,
        "purchase_value": purchase_value,
        "currency": _get_unit(pos.get("currentValue") or pos.get("kurswert"))
        or pos.get("currency")
        or "",
        "gains": gains,
        "gains_percent": gains_percent,
    }


def _map_depot_transaction(tx: dict) -> dict:
    raw_type = (
        tx.get("transactionType")
        or tx.get("transactionDirection")
        or tx.get("transaction_type")
        or ""
    ).upper()
    mapped_type = _DEPOT_TYPE_MAP.get(raw_type, "OTHER")

    instrument = tx.get("instrument") or {}
    amount_raw = _get_val(tx.get("transactionValue") or tx.get("amount"))

    return {
        "date": tx.get("bookingDate")
        or tx.get("transactionDate")
        or tx.get("booking_date")
        or "",
        "isin": instrument.get("isin") or tx.get("isin") or "",
        "wkn": instrument.get("wkn") or tx.get("wkn") or "",
        "name": instrument.get("name") or tx.get("name") or "",
        "transaction_type": mapped_type,
        "quantity": _get_val(tx.get("quantity") or tx.get("stueckzahl")),
        "price": _get_val(tx.get("price") or tx.get("kurs") or tx.get("current_price")),
        "amount": float(amount_raw),
        "currency": _get_unit(tx.get("transactionValue") or tx.get("amount"))
        or tx.get("currency")
        or "",
        "transaction_id": tx.get("transactionId") or "",
    }


def _depot_summary(positions: list[dict]) -> dict:
    total_value = sum(p["current_value"] for p in positions)
    total_purchase = sum(p["purchase_value"] for p in positions)
    total_gains = round(total_value - total_purchase, 2)
    total_gains_pct = round(
        (total_gains / total_purchase * 100) if total_purchase else 0, 4
    )
    return {
        "total_value": round(total_value, 2),
        "total_purchase_value": round(total_purchase, 2),
        "total_gains": total_gains,
        "total_gains_percent": total_gains_pct,
        "position_count": len(positions),
    }


def map_to_finance_agent(raw: dict) -> dict:
    """Transform raw get_all_data() output into the Finance Agent format."""
    accounts: list[dict] = raw.get("accounts") or []
    all_transactions: dict[str, list] = raw.get("transactions") or {}
    depots: list[dict] = raw.get("depots") or []
    depot_positions: dict[str, list] = raw.get("depot_positions") or {}
    depot_transactions_raw: dict[str, list] = raw.get("depot_transactions") or {}

    result: dict = {}
    total_tx_count = 0

    # --- Bank accounts ---
    for account in accounts:
        account_inner = account.get("account") or account
        account_id = account_inner.get("accountId") or ""
        canonical = _account_canonical_name(account)

        raw_txs = all_transactions.get(account_id) or []
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
            result[canonical]["summary"] = _compute_summary(
                result[canonical]["transactions"]
            )
        else:
            result[canonical] = entry

    # --- Depot ---
    if depots:
        depot = depots[0]
        depot_id = depot.get("depotId") or ""
        positions = [
            _map_depot_position(p) for p in (depot_positions.get(depot_id) or [])
        ]
        dep_txs = [
            _map_depot_transaction(tx)
            for tx in (depot_transactions_raw.get(depot_id) or [])
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
        "account_count": len(accounts),
        "total_transaction_count": total_tx_count,
    }

    return result
