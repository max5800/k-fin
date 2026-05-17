"""Tests for Comdirect API Pydantic models."""

from src.external.models import (
    ComdirectAccount,
    ComdirectData,
    ComdirectTransaction,
    DepotPosition,
    DepotTransaction,
)
from src.normalization.canonicalize import canonicalize


class TestComdirectAccount:
    def test_nested_shape(self):
        acc = ComdirectAccount.model_validate(
            {
                "account": {
                    "accountId": "123",
                    "iban": "DE00000000000000000000",
                    "accountType": {"key": "CURRENT_ACCOUNT"},
                },
                "balance": {"value": "1234.56", "unit": "EUR"},
            }
        )
        assert acc.account_id == "123"
        assert acc.iban == "DE00000000000000000000"
        assert acc.account_type == "CURRENT_ACCOUNT"
        assert acc.balance == 1234.56
        assert acc.currency == "EUR"

    def test_flat_shape(self):
        acc = ComdirectAccount.model_validate(
            {
                "accountId": "456",
                "iban": "DE11111111111111111111",
                "accountType": "SAVINGS_ACCOUNT",
                "balance": {"value": "500", "unit": "EUR"},
            }
        )
        assert acc.account_id == "456"
        assert acc.account_type == "SAVINGS_ACCOUNT"

    def test_missing_fields(self):
        acc = ComdirectAccount.model_validate({})
        assert acc.account_id == ""
        assert acc.balance == 0.0


class TestComdirectTransaction:
    def test_debit(self):
        tx = ComdirectTransaction.model_validate(
            {
                "transactionId": "tx1",
                "bookingDate": "2026-01-15",
                "transactionValue": {"value": "-42.50", "unit": "EUR"},
                "creditor": {"holderName": "REWE GmbH", "iban": "DE00000000000000000000"},
            }
        )
        assert tx.is_debit is True
        assert tx.counterpart_name == "REWE GmbH"
        assert tx.amount == -42.5

    def test_credit(self):
        # Comdirect delivers the incoming counterparty under `remitter`
        # (its `deptor` field — sic — is always empty).
        tx = ComdirectTransaction.model_validate(
            {
                "transactionValue": {"value": "1500", "unit": "EUR"},
                "remitter": {
                    "holderName": "Arbeitgeber GmbH",
                    "iban": "DE00000000000000000001",
                },
            }
        )
        assert tx.is_debit is False
        assert tx.counterpart_name == "Arbeitgeber GmbH"
        assert tx.counterpart_iban == "DE00000000000000000001"

    def test_credit_remitter_iban_reaches_canonical_sender(self):
        """The remitter IBAN must survive model → canonicalize as
        `sender_iban` — the field own-account-transfer detection pairs on."""
        tx = ComdirectTransaction.model_validate(
            {
                "transactionValue": {"value": "500", "unit": "EUR"},
                "remitter": {
                    "holderName": "John Doe",
                    "iban": "DE00000000000000000001",
                },
            }
        )
        canonical = canonicalize(tx.model_dump())
        assert canonical["sender"] == "John Doe"
        assert canonical["sender_iban"] == "DE00000000000000000001"

    def test_description_fallback(self):
        tx = ComdirectTransaction.model_validate(
            {
                "transactionValue": {},
                "typeText": "Dauerauftrag",
            }
        )
        assert tx.description == "Dauerauftrag"

    def test_empty_transaction(self):
        tx = ComdirectTransaction.model_validate({})
        assert tx.amount == 0.0
        assert tx.description == "Umsatz"


class TestDepotPosition:
    def test_with_instrument(self):
        pos = DepotPosition.model_validate(
            {
                "instrument": {
                    "isin": "IE00B4L5Y983",
                    "wkn": "A0RPWH",
                    "name": "iShares MSCI World",
                },
                "quantity": {"value": "10"},
                "currentValue": {"value": "900", "unit": "EUR"},
                "purchaseValue": {"value": "800", "unit": "EUR"},
            }
        )
        assert pos.isin == "IE00B4L5Y983"
        assert pos.quantity == 10.0
        assert pos.gains == 100.0
        assert pos.gains_percent == 12.5

    def test_german_field_names(self):
        pos = DepotPosition.model_validate(
            {
                "isin": "DE0005140008",
                "stueckzahl": "5",
                "kurswert": {"value": "500", "unit": "EUR"},
                "einstandswert": {"value": "450", "unit": "EUR"},
            }
        )
        assert pos.current_value == 500.0
        assert pos.purchase_value == 450.0

    def test_zero_purchase_no_division_error(self):
        pos = DepotPosition.model_validate(
            {
                "currentValue": {"value": "100"},
                "purchaseValue": {"value": "0"},
            }
        )
        assert pos.gains_percent == 0


class TestDepotTransaction:
    def test_buy(self):
        dtx = DepotTransaction.model_validate(
            {
                "transactionType": "BUY",
                "instrument": {"isin": "IE00B4L5Y983", "name": "iShares"},
                "quantity": {"value": "5"},
                "transactionValue": {"value": "400", "unit": "EUR"},
            }
        )
        assert dtx.transaction_type == "BUY"
        assert dtx.quantity == 5.0

    def test_german_type(self):
        dtx = DepotTransaction.model_validate({"transactionType": "Kauf"})
        assert dtx.transaction_type == "BUY"

    def test_unknown_type(self):
        dtx = DepotTransaction.model_validate({"transactionType": "SPLIT"})
        assert dtx.transaction_type == "OTHER"


class TestComdirectData:
    def test_parse_full_dataset(self):
        raw = {
            "accounts": [
                {
                    "account": {"accountId": "1", "iban": "DE00000000000000000000"},
                    "balance": {"value": "100"},
                },
            ],
            "transactions": {
                "1": [{"transactionId": "tx1", "transactionValue": {"value": "-10"}}],
            },
            "depots": [{"depotId": "d1"}],
            "depot_positions": {
                "d1": [
                    {
                        "instrument": {"isin": "IE00B4L5Y983"},
                        "currentValue": {"value": "500"},
                        "purchaseValue": {"value": "400"},
                    }
                ],
            },
            "depot_transactions": {
                "d1": [{"transactionType": "BUY", "transactionValue": {"value": "400"}}],
            },
        }
        data = ComdirectData.model_validate(raw)
        assert len(data.accounts) == 1
        assert data.accounts[0].account_id == "1"
        assert len(data.transactions["1"]) == 1
        assert data.transactions["1"][0].amount == -10.0
        assert len(data.depot_positions["d1"]) == 1
        assert data.depot_positions["d1"][0].isin == "IE00B4L5Y983"

    def test_empty_dataset(self):
        data = ComdirectData.model_validate({})
        assert data.accounts == []
        assert data.transactions == {}
