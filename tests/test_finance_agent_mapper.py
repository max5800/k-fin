"""Unit tests for src/exporter/finance_agent_mapper.py"""

import pytest
from src.exporter.finance_agent_mapper import map_to_finance_agent


def test_empty_input_returns_valid_structure():
    result = map_to_finance_agent({})

    assert "depot" in result
    assert result["depot"]["positions"] == []
    assert result["depot"]["transactions"] == []
    assert result["depot"]["summary"]["position_count"] == 0
    assert "meta" in result
    assert result["meta"]["account_count"] == 0
    assert result["meta"]["total_transaction_count"] == 0


def test_girokonto_transaction_mapped_correctly():
    raw = {
        "accounts": [
            {
                "account": {
                    "accountId": "ACC001",
                    "accountType": {"key": "CURRENT_ACCOUNT"},
                }
            }
        ],
        "transactions": {
            "ACC001": [
                {
                    "transactionId": "TX001",
                    "bookingDate": "2026-03-01",
                    "valutaDate": "2026-03-02",
                    "typeText": "Umsatz",
                    "remittanceInfo": "Supermarkt Einkauf",
                    "transactionValue": {"value": -45.50, "unit": "EUR"},
                    "creditor": {"holderName": "REWE GmbH", "iban": "DE00123456789"},
                    "debtor": {},
                }
            ]
        },
    }

    result = map_to_finance_agent(raw)

    assert "girokonto" in result
    giro = result["girokonto"]
    assert giro["account_id"] == "ACC001"
    assert len(giro["transactions"]) == 1

    tx = giro["transactions"][0]
    assert tx["date"] == "2026-03-01"
    assert tx["booking_date"] == "2026-03-01"
    assert tx["value_date"] == "2026-03-02"
    assert tx["amount"] == -45.50
    assert tx["currency"] == "EUR"
    assert tx["counterpart_name"] == "REWE GmbH"
    assert tx["counterpart_iban"] == "DE00123456789"
    assert tx["transaction_id"] == "TX001"
    assert tx["text"] == "Supermarkt Einkauf"

    summary = giro["summary"]
    assert summary["total_out"] == 45.50
    assert summary["total_in"] == 0.0
    assert summary["net"] == -45.50
    assert summary["count"] == 1


def test_depot_position_mapped_correctly():
    raw = {
        "depots": [{"depotId": "DEP001"}],
        "depot_positions": {
            "DEP001": [
                {
                    "instrument": {"isin": "DE0005140008", "wkn": "514000", "name": "Deutsche Bank AG"},
                    "quantity": {"value": 100.0},
                    "currentPrice": {"value": 12.50},
                    "currentValue": {"value": 1250.0, "unit": "EUR"},
                    "purchaseValue": {"value": 1100.0, "unit": "EUR"},
                }
            ]
        },
        "depot_transactions": {"DEP001": []},
    }

    result = map_to_finance_agent(raw)

    depot = result["depot"]
    assert depot["depot_id"] == "DEP001"
    assert len(depot["positions"]) == 1

    pos = depot["positions"][0]
    assert pos["isin"] == "DE0005140008"
    assert pos["wkn"] == "514000"
    assert pos["name"] == "Deutsche Bank AG"
    assert pos["quantity"] == 100.0
    assert pos["current_price"] == 12.50
    assert pos["current_value"] == 1250.0
    assert pos["purchase_value"] == 1100.0
    assert pos["gains"] == 150.0
    assert pos["currency"] == "EUR"

    summary = depot["summary"]
    assert summary["total_value"] == 1250.0
    assert summary["total_purchase_value"] == 1100.0
    assert summary["total_gains"] == 150.0
    assert summary["position_count"] == 1


@pytest.mark.parametrize(
    "raw_type, expected",
    [
        ("BUY", "BUY"),
        ("IN", "BUY"),
        ("KAUF", "BUY"),
        ("SELL", "SELL"),
        ("OUT", "SELL"),
        ("VERKAUF", "SELL"),
        ("DIVIDEND", "DIVIDEND"),
        ("ERTRAG", "DIVIDEND"),
        ("SPLIT", "OTHER"),
        ("", "OTHER"),
    ],
)
def test_depot_transaction_type_mapping(raw_type, expected):
    raw = {
        "depots": [{"depotId": "DEP001"}],
        "depot_transactions": {
            "DEP001": [
                {
                    "transactionId": "DTX001",
                    "transactionType": raw_type,
                    "bookingDate": "2026-03-01",
                    "instrument": {"isin": "DE000", "wkn": "000", "name": "Test AG"},
                    "quantity": {"value": 10.0},
                    "price": {"value": 100.0},
                    "transactionValue": {"value": -1000.0, "unit": "EUR"},
                }
            ]
        },
    }

    result = map_to_finance_agent(raw)
    tx = result["depot"]["transactions"][0]
    assert tx["transaction_type"] == expected
