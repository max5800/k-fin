"""Reliability tests for the anomaly-detection agent runner.

Like `test_categorization.py`, these tests stub `run_in_fresh_loop` so
no real LLM call happens. The fixture `gather_stub` replaces the four
`gather.*` helpers so the runner sees deterministic inputs without
needing a live Postgres.

The key behavioral guarantee verified here: there is **no early-return
floor**. Even when the statistical pre-filter finds zero outliers and
zero new counterparties, the agent still runs so the LLM can flag
subtle patterns the heuristics miss.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agents.types import AnomalyResult, Observation


@pytest.fixture
def anomaly_mod():
    """Lazy import — keeps the captured `settings` reference fresh.

    Mirrors `test_categorization.py::cat_mod`: `tests/auth/test_jwt.py`
    reloads `src.core.config` mid-suite, so a module-level import here
    would leave the runner with a stale singleton.
    """
    from src.agents import anomaly as _anomaly_mod

    return _anomaly_mod


def _fake_run_result(
    anomalies: list[Observation] | None = None,
    period: str = "2026-04-09/2026-05-09",
    new_counterparties: list[str] | None = None,
):
    """Mimic pydantic-ai AgentRunResult: `.output` carries the model."""
    output = AnomalyResult(
        anomalies=anomalies or [],
        period=period,
        total_anomalies=len(anomalies or []),
        new_counterparties=new_counterparties or [],
    )
    usage = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        request_tokens=0,
        response_tokens=0,
        total_tokens=0,
    )
    return SimpleNamespace(output=output, usage=lambda: usage)


@pytest.fixture
def gather_stub(monkeypatch, anomaly_mod):
    """Replace the gather.* helpers on the runner with controllable stubs.

    Returns a SimpleNamespace whose attributes are the mock objects so
    individual tests can configure their return values.
    """
    outliers = MagicMock(return_value=[])
    new_cps = MagicMock(return_value=[])
    period_txs = MagicMock(return_value=[])
    recent_reports = MagicMock(return_value=[])

    monkeypatch.setattr(anomaly_mod, "get_outlier_transactions", outliers)
    monkeypatch.setattr(anomaly_mod, "get_new_counterparties", new_cps)
    monkeypatch.setattr(anomaly_mod, "get_period_transactions", period_txs)
    monkeypatch.setattr(anomaly_mod, "get_recent_reports", recent_reports)

    return SimpleNamespace(
        outliers=outliers,
        new_counterparties=new_cps,
        period_transactions=period_txs,
        recent_reports=recent_reports,
    )


class TestNoFloor:
    """Empty outliers + empty new_counterparties must still invoke the LLM."""

    def test_empty_signals_still_runs_agent(
        self, monkeypatch, anomaly_mod, gather_stub
    ):
        """Regression: previously the runner short-circuited and skipped
        the LLM when both pre-filters were empty. The LLM must now run
        anyway so it can flag subtle patterns (missing recurring costs,
        gaps, trend breaks)."""
        # Pre-filter is "quiet" — no outliers, no new counterparties.
        gather_stub.outliers.return_value = []
        gather_stub.new_counterparties.return_value = []
        # …but there are transactions in the window for the LLM to look at.
        gather_stub.period_transactions.return_value = [
            {"id": "tx-1", "amount": -42.0, "recipient": "Edeka"},
            {"id": "tx-2", "amount": -19.99, "recipient": "Spotify"},
        ]

        run_calls: list[str] = []

        def fake_run(coro):
            try:
                coro.close()
            except Exception:
                pass
            run_calls.append("invoked")
            return _fake_run_result(anomalies=[])

        monkeypatch.setattr(anomaly_mod, "run_in_fresh_loop", fake_run)

        engine = MagicMock()
        result = anomaly_mod.run_anomaly_detection(
            engine,
            reference_date=date(2026, 5, 9),
            lookback_days=30,
        )

        # The LLM was called exactly once — no skipping.
        assert run_calls == ["invoked"]
        # And the result is the LLM's empty output, not a synthetic skip-result.
        assert isinstance(result, AnomalyResult)
        assert result.anomalies == []
        assert result.total_anomalies == 0
        assert result.period == "2026-04-09/2026-05-09"
        # `get_recent_reports` is consulted on the running path —
        # confirms we did not bail before assembling the prompt.
        gather_stub.recent_reports.assert_called_once()

    def test_outliers_present_runs_agent(
        self, monkeypatch, anomaly_mod, gather_stub
    ):
        """Sanity: the non-empty path must of course still run."""
        gather_stub.outliers.return_value = [
            {"id": "tx-99", "amount": -2500.0, "recipient": "Unknown GmbH"}
        ]
        gather_stub.new_counterparties.return_value = []
        gather_stub.period_transactions.return_value = []

        invoked = {"n": 0}

        def fake_run(coro):
            try:
                coro.close()
            except Exception:
                pass
            invoked["n"] += 1
            return _fake_run_result(
                anomalies=[
                    Observation(
                        category="anomaly",
                        summary="Hohe Buchung an unbekannte Gegenpartei",
                        severity="warning",
                        transaction_ids=["tx-99"],
                    )
                ]
            )

        monkeypatch.setattr(anomaly_mod, "run_in_fresh_loop", fake_run)

        engine = MagicMock()
        result = anomaly_mod.run_anomaly_detection(
            engine, reference_date=date(2026, 5, 9)
        )

        assert invoked["n"] == 1
        assert result.total_anomalies == 1
        assert result.anomalies[0].transaction_ids == ["tx-99"]
