"""Tests for the M7 Agent Orchestrator.

Covers:
- gather.py: DB query helpers against testcontainers Postgres
- categorization agent: pydantic-ai TestModel (no LLM calls)
- orchestrator: full/single run lifecycle, partial failure
- API endpoints: POST /runs/{agent}, POST /runs/full
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from src.core.db.models import (
    AgentRun,
    Category,
    NormalizedTransaction,
    RawTransaction,
    RecurringPattern,
    Report,
    ReportStatus,
    RunStatus,
    TypeEnum,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_seed(db_engine):
    """Seed DB with data suitable for agent testing."""
    with Session(db_engine) as s:
        # Categories
        s.add(Category(id="groceries", name="Lebensmittel", type=TypeEnum.VARIABEL))
        s.add(Category(id="rent", name="Miete", type=TypeEnum.FIX))
        s.add(Category(id="fun", name="Freizeit", type=TypeEnum.DISKRETIONAER))

        # Raw transactions (FK target)
        for char in "abcde":
            s.add(
                RawTransaction(
                    content_hash=char * 64,
                    comdirect_id=f"CD-{char}",
                    raw_data={"stub": True},
                )
            )
        s.flush()

        # Categorized transactions
        s.add(
            NormalizedTransaction(
                id="txn-cat-1",
                raw_content_hash="a" * 64,
                booking_date=date(2026, 4, 7),
                valuation_date=date(2026, 4, 7),
                amount=Decimal("-42.50"),
                sender="John Doe",
                recipient="REWE",
                description="Einkauf REWE",
                category_id="groceries",
                is_recurring=False,
                is_outlier=False,
                internal_transfer=False,
            )
        )
        s.add(
            NormalizedTransaction(
                id="txn-cat-2",
                raw_content_hash="b" * 64,
                booking_date=date(2026, 4, 1),
                valuation_date=date(2026, 4, 1),
                amount=Decimal("-850.00"),
                sender="John Doe",
                recipient="Vermieter GmbH",
                description="Miete April",
                category_id="rent",
                is_recurring=True,
                is_outlier=False,
                internal_transfer=False,
            )
        )

        # Uncategorized transaction (target for categorization agent)
        s.add(
            NormalizedTransaction(
                id="txn-uncat-1",
                raw_content_hash="c" * 64,
                booking_date=date(2026, 4, 5),
                valuation_date=date(2026, 4, 5),
                amount=Decimal("-15.90"),
                sender="John Doe",
                recipient="Bäckerei Schmidt",
                description="Brötchen und Kaffee",
                category_id=None,
                is_recurring=False,
                is_outlier=False,
                internal_transfer=False,
            )
        )

        # Income
        s.add(
            NormalizedTransaction(
                id="txn-income-1",
                raw_content_hash="d" * 64,
                booking_date=date(2026, 4, 1),
                valuation_date=date(2026, 4, 1),
                amount=Decimal("3500.00"),
                sender="Arbeitgeber AG",
                recipient="John Doe",
                description="Gehalt April",
                category_id=None,
                is_recurring=True,
                is_outlier=False,
                internal_transfer=False,
            )
        )

        # Outlier
        s.add(
            NormalizedTransaction(
                id="txn-outlier-1",
                raw_content_hash="e" * 64,
                booking_date=date(2026, 4, 10),
                valuation_date=date(2026, 4, 10),
                amount=Decimal("-1271.00"),
                sender="John Doe",
                recipient="PayPal Europe",
                description="PayPal Zahlung",
                category_id=None,
                is_recurring=False,
                is_outlier=True,
                internal_transfer=False,
            )
        )

        # Recurring pattern
        s.add(
            RecurringPattern(
                recipient="Vermieter GmbH",
                avg_amount=Decimal("-850.00"),
                amount_stddev=Decimal("0.00"),
                first_seen_month=date(2025, 1, 1),
                last_seen_month=date(2026, 4, 1),
                occurrence_count=16,
            )
        )

        # A previous report (for memory testing)
        s.add(
            Report(
                id="prev-report-001",
                report_type="weekly_analysis",
                title="weekly_analysis — 2026-W14",
                content={
                    "observations": [],
                    "period": "2026-W14",
                    "summary_text": "Ruhige Woche.",
                },
                period_start=date(2026, 4, 6),
                period_end=date(2026, 4, 12),
                format="json",
                status=ReportStatus.READY,
            )
        )

        s.commit()


# ---------------------------------------------------------------------------
# gather.py tests
# ---------------------------------------------------------------------------


class TestGather:
    def test_get_uncategorized_transactions(self, db_engine, agent_seed):
        from src.agents.gather import get_uncategorized_transactions

        result = get_uncategorized_transactions(db_engine)
        # txn-uncat-1, txn-income-1, txn-outlier-1 are uncategorized
        ids = {r["id"] for r in result}
        assert "txn-uncat-1" in ids
        assert "txn-outlier-1" in ids
        assert "txn-cat-1" not in ids  # has category

    def test_get_available_categories(self, db_engine, agent_seed):
        from src.agents.gather import get_available_categories

        result = get_available_categories(db_engine)
        names = {c["name"] for c in result}
        assert names == {"Lebensmittel", "Miete", "Freizeit"}

    def test_get_monthly_summary(self, db_engine, agent_seed):
        from src.agents.gather import get_monthly_summary

        result = get_monthly_summary(db_engine, months=3)
        assert len(result) >= 1
        april = [r for r in result if r["month"] == "2026-04"]
        assert len(april) == 1
        assert april[0]["income"] > 0
        assert april[0]["expenses"] < 0

    def test_get_category_breakdown(self, db_engine, agent_seed):
        from src.agents.gather import get_category_breakdown

        result = get_category_breakdown(db_engine)
        groceries = [r for r in result if r["category_id"] == "groceries"]
        assert len(groceries) == 1
        assert groceries[0]["count"] == 1

    def test_get_period_transactions(self, db_engine, agent_seed):
        from src.agents.gather import get_period_transactions

        result = get_period_transactions(db_engine, date(2026, 4, 1), date(2026, 4, 30))
        assert len(result) == 5
        assert all("id" in r for r in result)

    def test_get_outlier_transactions(self, db_engine, agent_seed):
        from src.agents.gather import get_outlier_transactions

        result = get_outlier_transactions(
            db_engine, date(2026, 4, 1), date(2026, 4, 30)
        )
        assert len(result) == 1
        assert result[0]["id"] == "txn-outlier-1"

    def test_get_new_counterparties(self, db_engine, agent_seed):
        from src.agents.gather import get_new_counterparties

        # All recipients are "new" since 2026-04-01
        result = get_new_counterparties(db_engine, date(2026, 4, 1))
        assert isinstance(result, list)

    def test_get_savings_rate(self, db_engine, agent_seed):
        from src.agents.gather import get_savings_rate

        result = get_savings_rate(db_engine, date(2026, 4, 1), date(2026, 4, 30))
        assert result["income"] == 3500.0
        assert result["savings_rate_pct"] > 0

    def test_get_recent_reports(self, db_engine, agent_seed):
        from src.agents.gather import get_recent_reports

        result = get_recent_reports(db_engine, "weekly_analysis", limit=3)
        assert len(result) == 1
        assert result[0]["content"]["period"] == "2026-W14"

    def test_get_recurring_patterns(self, db_engine, agent_seed):
        from src.agents.gather import get_recurring_patterns

        result = get_recurring_patterns(db_engine)
        assert len(result) == 1
        assert result[0]["recipient"] == "Vermieter GmbH"


# ---------------------------------------------------------------------------
# Categorization agent tests (TestModel — no LLM)
# ---------------------------------------------------------------------------


class TestCategorizationAgent:
    def test_empty_uncategorized_skips_llm(self, db_engine):
        """When all transactions have categories, no LLM call is made."""
        from src.agents.categorization import run_categorization

        # Empty DB → no uncategorized transactions
        result = run_categorization(db_engine)
        assert result.suggestions == []
        assert result.uncategorized_count == 0

    def test_no_categories_skips_llm(self, db_engine, agent_seed):
        """When no categories exist, skip gracefully."""
        from sqlalchemy import update

        with Session(db_engine) as s:
            from src.core.db.models import NormalizedTransaction as NT

            s.execute(update(NT).values(category_id=None))
            s.commit()
            from src.core.db.models import Category as Cat
            from src.core.db.models import Rule

            s.query(Rule).delete()
            s.query(Cat).delete()
            s.commit()

        from src.agents.categorization import run_categorization

        result = run_categorization(db_engine)
        assert result.suggestions == []
        assert result.uncategorized_count > 0

    def test_categorization_with_test_model(self, db_engine, agent_seed):
        """Run categorization with pydantic-ai TestModel."""
        from pydantic_ai.models.test import TestModel

        from src.agents.categorization import categorization_agent, run_categorization
        from src.agents.types import CategorizationResult

        with categorization_agent.override(model=TestModel()):
            result = run_categorization(db_engine)

        assert isinstance(result, CategorizationResult)


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------


class TestOrchestrator:
    def test_run_single_creates_run_and_report(self, db_engine, agent_seed):
        """run_single creates an AgentRun and a Report."""
        from pydantic_ai.models.test import TestModel

        from src.agents.categorization import categorization_agent
        from src.agents.orchestrator import AgentOrchestrator

        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.engine = db_engine
        orchestrator.own_ibans = []

        with categorization_agent.override(model=TestModel()):
            run_id = orchestrator.run_single("categorization")

        with Session(db_engine) as s:
            run = s.get(AgentRun, run_id)
            assert run is not None
            assert run.agent_name == "categorization"
            assert run.status == RunStatus.SUCCEEDED

            reports = (
                s.query(Report)
                .filter(
                    Report.report_type == "categorization",
                    Report.id != "prev-report-001",
                )
                .all()
            )
            assert len(reports) == 1
            assert reports[0].report_type == "categorization"
            assert reports[0].content is not None

    def test_run_single_invalid_type_raises(self, db_engine):
        """run_single with invalid type raises ValueError."""
        from src.agents.orchestrator import AgentOrchestrator

        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.engine = db_engine
        orchestrator.own_ibans = []

        with pytest.raises(ValueError, match="Invalid agent type"):
            orchestrator.run_single("nonexistent")

    def test_run_full_creates_multiple_reports(self, db_engine, agent_seed):
        """run_full creates reports for all agents."""
        from pydantic_ai.models.test import TestModel

        from src.agents.anomaly import anomaly_agent
        from src.agents.categorization import categorization_agent
        from src.agents.monthly_analysis import monthly_analysis_agent
        from src.agents.orchestrator import AgentOrchestrator
        from src.agents.synthesizer import synthesizer_agent
        from src.agents.weekly_analysis import weekly_analysis_agent

        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.engine = db_engine
        orchestrator.own_ibans = []

        with (
            categorization_agent.override(model=TestModel()),
            weekly_analysis_agent.override(model=TestModel()),
            monthly_analysis_agent.override(model=TestModel()),
            anomaly_agent.override(model=TestModel()),
            synthesizer_agent.override(model=TestModel()),
        ):
            run_id = orchestrator.run_full()

        with Session(db_engine) as s:
            run = s.get(AgentRun, run_id)
            assert run is not None
            assert run.agent_name == "full"
            assert run.status == RunStatus.SUCCEEDED

            # 5 new reports + 1 seed report
            new_reports = s.query(Report).filter(Report.id != "prev-report-001").all()
            report_types = {r.report_type for r in new_reports}
            assert "categorization" in report_types
            assert "synthesis" in report_types
            assert len(new_reports) == 5

    def test_run_single_for_reuses_existing_run(self, db_engine, agent_seed):
        """run_single_for picks up an existing PENDING run."""
        from pydantic_ai.models.test import TestModel

        from src.agents.categorization import categorization_agent
        from src.agents.orchestrator import AgentOrchestrator

        orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
        orchestrator.engine = db_engine
        orchestrator.own_ibans = []

        # Pre-create a PENDING run (as the API would)
        run_id = "test-run-001"
        with Session(db_engine) as s:
            s.add(
                AgentRun(
                    id=run_id,
                    agent_name="categorization",
                    status=RunStatus.PENDING,
                    trigger="manual",
                    started_at=datetime.now(timezone.utc),
                )
            )
            s.commit()

        with categorization_agent.override(model=TestModel()):
            orchestrator.run_single_for(run_id, "categorization")

        with Session(db_engine) as s:
            run = s.get(AgentRun, run_id)
            assert run.status == RunStatus.SUCCEEDED
            assert run.finished_at is not None


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_api_client(db_engine):
    """TestClient for agent API endpoints."""
    from fastapi.testclient import TestClient

    from src.api.deps import get_db

    def _override_get_db():
        with Session(db_engine) as session:
            yield session

    with patch.dict(os.environ, {"API_TOKEN": "test-secret"}):
        from src.api.app import create_app

        app = create_app()
        app.dependency_overrides[get_db] = _override_get_db
        client = TestClient(app)
        yield client
        app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer test-secret"}


class TestAgentAPI:
    def test_start_run_unknown_agent_returns_404(self, agent_api_client):
        resp = agent_api_client.post("/api/v1/runs/bogus", headers=AUTH)
        assert resp.status_code == 404
        assert "Unknown agent" in resp.json()["detail"]

    def test_start_run_returns_pending(self, agent_api_client):
        resp = agent_api_client.post("/api/v1/runs/categorization", headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_name"] == "categorization"
        assert data["status"] == "pending"

    def test_start_full_run_returns_pending(self, agent_api_client):
        resp = agent_api_client.post("/api/v1/runs/full", headers=AUTH)
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_name"] == "full"
        assert data["status"] == "pending"

    def test_list_runs(self, agent_api_client):
        # Create a run first
        agent_api_client.post("/api/v1/runs/anomaly", headers=AUTH)
        resp = agent_api_client.get("/api/v1/runs", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1

    def test_get_run_by_id(self, agent_api_client):
        create_resp = agent_api_client.post("/api/v1/runs/categorization", headers=AUTH)
        run_id = create_resp.json()["id"]

        resp = agent_api_client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["id"] == run_id

    def test_get_run_not_found(self, agent_api_client):
        resp = agent_api_client.get("/api/v1/runs/nonexistent", headers=AUTH)
        assert resp.status_code == 404
