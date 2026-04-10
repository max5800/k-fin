"""Tests for the model-based JSON export."""

from datetime import date

from src.exporter.json_export import build_export


def _sample_raw() -> dict:
    """Minimal raw data matching ComdirectClient.get_all_data() shape."""
    return {
        "accounts": [
            {
                "account": {
                    "accountId": "ACC1",
                    "iban": "DE00000000000000000000",
                    "accountType": {"key": "CURRENT_ACCOUNT"},
                },
                "balance": {"value": "2500.00", "unit": "EUR"},
            },
        ],
        "transactions": {
            "ACC1": [
                {
                    "transactionId": "TX1",
                    "bookingDate": "2026-03-01",
                    "valutaDate": "2026-03-01",
                    "transactionValue": {"value": "-42.50", "unit": "EUR"},
                    "typeText": "Lastschrift",
                    "remittanceInfo": "REWE Einkauf",
                    "creditor": {"holderName": "REWE GmbH", "iban": "DE00000000000000000000"},
                },
                {
                    "transactionId": "TX2",
                    "bookingDate": "2026-04-01",
                    "transactionValue": {"value": "3200.00", "unit": "EUR"},
                    "typeText": "Gehalt",
                    "debtor": {"holderName": "John Doe Arbeitgeber GmbH"},
                },
            ],
        },
        "depots": [{"depotId": "DEP1"}],
        "depot_positions": {
            "DEP1": [
                {
                    "instrument": {"isin": "IE00B4L5Y983", "wkn": "A0RPWH", "name": "iShares MSCI World"},
                    "quantity": {"value": "10"},
                    "currentPrice": {"value": "95.00"},
                    "currentValue": {"value": "950.00", "unit": "EUR"},
                    "purchaseValue": {"value": "800.00", "unit": "EUR"},
                },
            ],
        },
        "depot_transactions": {
            "DEP1": [
                {
                    "transactionId": "DTX1",
                    "bookingDate": "2026-03-15",
                    "transactionType": "BUY",
                    "instrument": {"isin": "IE00B4L5Y983", "name": "iShares MSCI World"},
                    "quantity": {"value": "5"},
                    "price": {"value": "93.00"},
                    "transactionValue": {"value": "-465.00", "unit": "EUR"},
                },
            ],
        },
    }


class TestBuildExport:
    def test_full_export(self):
        result = build_export(_sample_raw())

        assert result["meta"]["format"] == "comdirect-export"
        assert result["meta"]["format_version"] == "1.0"
        assert result["meta"]["account_count"] == 1
        assert result["meta"]["transaction_count"] == 2
        assert result["meta"]["position_count"] == 1
        assert result["meta"]["depot_transaction_count"] == 1
        assert result["meta"]["since"] is None

        # Account data comes through models
        assert len(result["accounts"]) == 1
        acc = result["accounts"][0]
        assert acc["account_id"] == "ACC1"
        assert acc["iban"] == "DE00000000000000000000"
        assert acc["balance"] == 2500.0

        # Transactions are keyed by account_id
        txs = result["transactions"]["ACC1"]
        assert len(txs) == 2
        assert txs[0]["amount"] == -42.5
        assert txs[1]["amount"] == 3200.0

        # Depot positions
        positions = result["depot_positions"]["DEP1"]
        assert len(positions) == 1
        assert positions[0]["isin"] == "IE00B4L5Y983"
        assert positions[0]["current_value"] == 950.0

    def test_since_filters_transactions(self):
        result = build_export(_sample_raw(), since=date(2026, 3, 15))

        # TX1 (2026-03-01) should be filtered out, TX2 (2026-04-01) kept
        txs = result["transactions"]["ACC1"]
        assert len(txs) == 1
        assert txs[0]["transaction_id"] == "TX2"

        assert result["meta"]["transaction_count"] == 1
        assert result["meta"]["since"] == "2026-03-15"

    def test_since_filters_depot_transactions(self):
        result = build_export(_sample_raw(), since=date(2026, 4, 1))

        # DTX1 (2026-03-15) should be filtered out
        depot_txs = result["depot_transactions"]["DEP1"]
        assert len(depot_txs) == 0
        assert result["meta"]["depot_transaction_count"] == 0

    def test_empty_dataset(self):
        result = build_export({})

        assert result["accounts"] == []
        assert result["transactions"] == {}
        assert result["meta"]["account_count"] == 0
        assert result["meta"]["transaction_count"] == 0

    def test_unbooked_transactions_kept_with_since(self):
        """Transactions without a booking_date are always included."""
        raw = _sample_raw()
        raw["transactions"]["ACC1"].append({
            "transactionId": "TX_OPEN",
            "bookingDate": "",
            "transactionValue": {"value": "-15.00", "unit": "EUR"},
        })
        result = build_export(raw, since=date(2026, 12, 31))

        tx_ids = [tx["transaction_id"] for tx in result["transactions"]["ACC1"]]
        assert "TX_OPEN" in tx_ids
