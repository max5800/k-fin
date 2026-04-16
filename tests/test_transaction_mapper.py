"""Tests for Firefly III transaction mapper."""

from src.importer.transaction_mapper import map_transaction


def _make_tx(
    amount: str = "-42.50",
    currency: str = "EUR",
    booking_date: str = "2026-01-15",
    remittance_info: str = "REWE Einkauf",
    type_text: str = "Lastschrift",
    transaction_id: str = "tx-001",
    creditor_name: str = "REWE GmbH",
    creditor_iban: str = "DE00000000000000000000",
    debtor_name: str = "",
    debtor_iban: str = "",
) -> dict:
    return {
        "transactionId": transaction_id,
        "bookingDate": booking_date,
        "valutaDate": booking_date,
        "transactionValue": {"value": amount, "unit": currency},
        "typeText": type_text,
        "remittanceInfo": remittance_info,
        "creditor": {"holderName": creditor_name, "iban": creditor_iban},
        "debtor": {"holderName": debtor_name, "iban": debtor_iban},
    }


FIREFLY_ACCOUNT_ID = "99"


class TestMapTransaction:
    def test_withdrawal(self):
        result = map_transaction(_make_tx(amount="-42.50"), FIREFLY_ACCOUNT_ID)
        tx = result["transactions"][0]
        assert tx["type"] == "withdrawal"
        assert tx["amount"] == "42.5"
        assert tx["source_id"] == FIREFLY_ACCOUNT_ID
        assert tx["destination_name"] == "REWE GmbH"
        assert tx["destination_iban"] == "DE00000000000000000000"

    def test_deposit(self):
        result = map_transaction(
            _make_tx(
                amount="1500.00",
                debtor_name="Arbeitgeber GmbH",
                debtor_iban="DE11111111111111111111",
            ),
            FIREFLY_ACCOUNT_ID,
        )
        tx = result["transactions"][0]
        assert tx["type"] == "deposit"
        assert tx["amount"] == "1500.0"
        assert tx["destination_id"] == FIREFLY_ACCOUNT_ID
        assert tx["source_name"] == "Arbeitgeber GmbH"

    def test_zero_amount(self):
        result = map_transaction(_make_tx(amount="0"), FIREFLY_ACCOUNT_ID)
        tx = result["transactions"][0]
        assert tx["amount"] == "0.0"
        assert tx["type"] == "deposit"

    def test_external_id_preserved(self):
        result = map_transaction(_make_tx(transaction_id="abc-123"), FIREFLY_ACCOUNT_ID)
        tx = result["transactions"][0]
        assert tx["external_id"] == "abc-123"
        assert "abc-123" in tx["notes"]

    def test_description_from_remittance(self):
        result = map_transaction(_make_tx(remittance_info="Miete Januar"), FIREFLY_ACCOUNT_ID)
        assert result["transactions"][0]["description"] == "Miete Januar"

    def test_description_fallback_to_type_text(self):
        result = map_transaction(
            _make_tx(remittance_info="", type_text="Dauerauftrag"), FIREFLY_ACCOUNT_ID
        )
        assert result["transactions"][0]["description"] == "Dauerauftrag"

    def test_description_truncated_at_255(self):
        long_text = "A" * 300
        result = map_transaction(_make_tx(remittance_info=long_text), FIREFLY_ACCOUNT_ID)
        assert len(result["transactions"][0]["description"]) == 255

    def test_missing_fields_use_defaults(self):
        minimal = {"transactionValue": {}, "creditor": {}, "debtor": {}}
        result = map_transaction(minimal, FIREFLY_ACCOUNT_ID)
        tx = result["transactions"][0]
        assert tx["amount"] == "0.0"
        assert tx["currency_code"] == "EUR"

    def test_dedup_flag_set(self):
        result = map_transaction(_make_tx(), FIREFLY_ACCOUNT_ID)
        assert result["error_if_duplicate_hash"] is True

    def test_tags_include_sync_marker(self):
        result = map_transaction(_make_tx(), FIREFLY_ACCOUNT_ID)
        assert "comdirect-sync" in result["transactions"][0]["tags"]
