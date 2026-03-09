"""Maps Comdirect transactions to Firefly III format."""

from datetime import datetime


def map_transaction(comdirect_tx: dict, firefly_account_id: str) -> dict:
    """
    Convert a Comdirect transaction to Firefly III transaction payload.

    Comdirect fields (simplified):
    - transactionId, bookingDate, valutaDate
    - transactionValue.value, transactionValue.unit (currency)
    - remittanceInfo, typeText
    - creditor / debtor (name, IBAN)

    Returns a Firefly III /api/v1/transactions POST payload.
    """
    amount = abs(float(comdirect_tx.get("transactionValue", {}).get("value", 0)))
    is_debit = float(comdirect_tx.get("transactionValue", {}).get("value", 0)) < 0
    currency = comdirect_tx.get("transactionValue", {}).get("unit", "EUR")
    booking_date = comdirect_tx.get("bookingDate", datetime.utcnow().strftime("%Y-%m-%d"))
    description = (
        comdirect_tx.get("remittanceInfo", "")
        or comdirect_tx.get("typeText", "Umsatz")
        or "Umsatz"
    )[:255]
    external_id = comdirect_tx.get("transactionId", "")
    counterpart = comdirect_tx.get("creditor" if is_debit else "debtor", {})
    counterpart_name = counterpart.get("holderName", "Unbekannt")
    counterpart_iban = counterpart.get("iban", "")

    tx_type = "withdrawal" if is_debit else "deposit"

    payload = {
        "error_if_duplicate_hash": True,
        "apply_rules": True,
        "transactions": [
            {
                "type": tx_type,
                "date": booking_date,
                "amount": str(amount),
                "currency_code": currency,
                "description": description,
                "external_id": external_id,
                "source_id": firefly_account_id if is_debit else None,
                "destination_id": firefly_account_id if not is_debit else None,
                "destination_name": counterpart_name if is_debit else None,
                "source_name": counterpart_name if not is_debit else None,
                "destination_iban": counterpart_iban if is_debit else None,
                "source_iban": counterpart_iban if not is_debit else None,
                "tags": ["comdirect-sync"],
                "notes": f"Importiert via comdirect-firefly-sync | comdirect ID: {external_id}",
            }
        ],
    }
    return payload
