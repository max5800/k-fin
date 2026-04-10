"""Model-based JSON export for Comdirect financial data.

Uses the Pydantic models from src.connector.models as the single source of
truth — no manual field-picking, no raw-dict navigation.  The output is
a faithful ``ComdirectData.model_dump()`` enriched with a ``meta`` block
that downstream agent / report consumers can rely on.
"""

from datetime import date, datetime, timezone

from src.connector.models import ComdirectData


def build_export(
    raw: dict,
    *,
    since: date | None = None,
) -> dict:
    """Parse *raw* through Pydantic models and return an export-ready dict.

    Parameters
    ----------
    raw:
        The dict returned by ``ComdirectClient.get_all_data()``.
    since:
        Optional lower bound on ``booking_date``.  Transactions before this
        date are excluded from the output (open / un-booked transactions are
        always kept).
    """
    data = ComdirectData.model_validate(raw)

    # Optional date filtering
    if since:
        since_str = since.isoformat()
        data.transactions = {
            acct: [
                tx for tx in txs
                if not tx.booking_date or tx.booking_date >= since_str
            ]
            for acct, txs in data.transactions.items()
        }
        data.depot_transactions = {
            depot: [
                tx for tx in txs
                if not tx.booking_date or tx.booking_date >= since_str
            ]
            for depot, txs in data.depot_transactions.items()
        }

    payload = data.model_dump()

    # Count totals for meta block
    tx_count = sum(len(txs) for txs in data.transactions.values())
    depot_tx_count = sum(len(txs) for txs in data.depot_transactions.values())
    position_count = sum(len(ps) for ps in data.depot_positions.values())

    payload["meta"] = {
        "format": "comdirect-export",
        "format_version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account_count": len(data.accounts),
        "transaction_count": tx_count,
        "depot_count": len(data.depots),
        "position_count": position_count,
        "depot_transaction_count": depot_tx_count,
        "since": since.isoformat() if since else None,
    }

    return payload
