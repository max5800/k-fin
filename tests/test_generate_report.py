"""Unit tests for scripts/generate_report.py"""

from scripts.generate_report import generate_report, _fmt_eur, _fmt_pct


def _minimal_data(**overrides):
    """Build minimal Finance Agent format data with optional overrides."""
    base = {
        "girokonto": {
            "account_id": "ACC001",
            "transactions": [
                {
                    "date": "2026-04-01",
                    "booking_date": "2026-04-01",
                    "value_date": "2026-04-01",
                    "type": "Gehalt",
                    "text": "Gehalt",
                    "amount": 3000.00,
                    "currency": "EUR",
                    "counterpart_name": "Arbeitgeber GmbH",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX001",
                },
                {
                    "date": "2026-04-02",
                    "booking_date": "2026-04-02",
                    "value_date": "2026-04-02",
                    "type": "Lastschrift",
                    "text": "Miete",
                    "amount": -800.00,
                    "currency": "EUR",
                    "counterpart_name": "Vermieter",
                    "counterpart_iban": "DE00000000000000000000",
                    "transaction_id": "TX002",
                },
            ],
            "summary": {"total_in": 3000.00, "total_out": 800.00, "net": 2200.00, "count": 2},
        },
        "depot": {
            "depot_id": "DEP001",
            "positions": [],
            "transactions": [],
            "summary": {
                "total_value": 0,
                "total_purchase_value": 0,
                "total_gains": 0,
                "total_gains_percent": 0,
                "position_count": 0,
            },
        },
        "meta": {
            "exported_at": "2026-04-10T08:00:00+00:00",
            "account_count": 1,
            "total_transaction_count": 2,
        },
    }
    base.update(overrides)
    return base


class TestFormatHelpers:
    def test_fmt_eur_positive(self):
        assert _fmt_eur(1234.50) == "1.234,50 EUR"

    def test_fmt_eur_negative(self):
        assert _fmt_eur(-42.00) == "-42,00 EUR"

    def test_fmt_eur_zero(self):
        assert _fmt_eur(0) == "0,00 EUR"

    def test_fmt_eur_large(self):
        assert _fmt_eur(1000000.00) == "1.000.000,00 EUR"

    def test_fmt_pct_positive(self):
        assert _fmt_pct(13.636) == "+13,64 %"

    def test_fmt_pct_negative(self):
        assert _fmt_pct(-3.5) == "-3,50 %"


class TestGenerateReport:
    def test_report_contains_header(self):
        report = generate_report(_minimal_data(), report_date="2026-04-10")
        assert "# Finanzreport — 2026-04-10" in report

    def test_report_contains_account_table(self):
        report = generate_report(_minimal_data())
        assert "Kontenübersicht" in report
        assert "Girokonto" in report
        assert "3.000,00 EUR" in report

    def test_report_contains_top_spending(self):
        report = generate_report(_minimal_data())
        assert "Top-Ausgaben" in report
        assert "Vermieter" in report

    def test_report_contains_largest_transactions(self):
        report = generate_report(_minimal_data())
        assert "Größte Einzelbuchungen" in report
        assert "Gehalt" in report

    def test_report_with_depot(self):
        data = _minimal_data(
            depot={
                "depot_id": "DEP001",
                "positions": [
                    {
                        "isin": "IE00B4L5Y983",
                        "wkn": "A0RPWH",
                        "name": "iShares MSCI World",
                        "quantity": 10.0,
                        "current_price": 95.0,
                        "current_value": 950.0,
                        "purchase_value": 800.0,
                        "gains": 150.0,
                        "gains_percent": 18.75,
                        "currency": "EUR",
                    }
                ],
                "transactions": [],
                "summary": {
                    "total_value": 950.0,
                    "total_purchase_value": 800.0,
                    "total_gains": 150.0,
                    "total_gains_percent": 18.75,
                    "position_count": 1,
                },
            }
        )
        report = generate_report(data)
        assert "Depot" in report
        assert "iShares MSCI World" in report
        assert "IE00B4L5Y983" in report

    def test_empty_data_does_not_crash(self):
        report = generate_report({"meta": {}, "depot": {"positions": [], "transactions": [], "summary": {}}})
        assert "Finanzreport" in report

    def test_recurring_counterparts(self):
        """Counterparts with 3+ transactions should appear in recurring section."""
        txs = [
            {
                "date": f"2026-04-0{i}",
                "booking_date": f"2026-04-0{i}",
                "value_date": f"2026-04-0{i}",
                "type": "Lastschrift",
                "text": "Einkauf",
                "amount": -30.00,
                "currency": "EUR",
                "counterpart_name": "REWE GmbH",
                "counterpart_iban": "DE00000000000000000000",
                "transaction_id": f"TX{i}",
            }
            for i in range(1, 5)
        ]
        data = _minimal_data()
        data["girokonto"]["transactions"] = txs
        data["girokonto"]["summary"]["count"] = 4

        report = generate_report(data)
        assert "Wiederkehrende" in report
        assert "REWE GmbH" in report
        assert "4x" in report

    def test_report_footer(self):
        report = generate_report(_minimal_data(), report_date="2026-04-10")
        assert "comdirect-firefly-sync" in report
