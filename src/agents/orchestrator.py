"""Agent Orchestrator — sequences agents and persists results.

Follows the same SyncRun lifecycle pattern as NormalizationPipeline:
create run → execute → persist results → finish run.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, create_engine, update
from sqlalchemy.orm import Session

from src.agents.anomaly import run_anomaly_detection
from src.agents.categorization import run_categorization
from src.agents.monthly_analysis import run_monthly_analysis
from src.agents.synthesizer import run_synthesizer
from src.agents.weekly_analysis import run_weekly_analysis
from src.core.db.models import AgentRun, RunStatus, RunTrigger

logger = logging.getLogger(__name__)

VALID_AGENT_TYPES = frozenset({
    "categorization",
    "weekly_analysis",
    "monthly_analysis",
    "anomaly",
    "synthesis",
})


class AgentOrchestrator:
    def __init__(self, database_url: str, own_ibans: list[str] | None = None):
        self.engine: Engine = create_engine(database_url)
        self.own_ibans = list(own_ibans or [])

    def run_full(self) -> str:
        """Run all agents in sequence, return the SyncRun ID."""
        run_id = str(uuid.uuid4())
        self._create_run(run_id, agent_type="full")

        results: dict[str, Any] = {}
        errors: list[str] = []

        # 1. Categorization
        try:
            results["categorization"] = run_categorization(self.engine)
        except Exception as exc:
            logger.error("Categorization agent failed: %s", exc)
            errors.append(f"categorization: {exc}")

        # 2. Weekly analysis
        try:
            results["weekly_analysis"] = run_weekly_analysis(self.engine)
        except Exception as exc:
            logger.error("Weekly analysis agent failed: %s", exc)
            errors.append(f"weekly_analysis: {exc}")

        # 3. Monthly analysis
        try:
            results["monthly_analysis"] = run_monthly_analysis(self.engine)
        except Exception as exc:
            logger.error("Monthly analysis agent failed: %s", exc)
            errors.append(f"monthly_analysis: {exc}")

        # 4. Anomaly detection
        try:
            results["anomaly"] = run_anomaly_detection(self.engine)
        except Exception as exc:
            logger.error("Anomaly agent failed: %s", exc)
            errors.append(f"anomaly: {exc}")

        # 5. Synthesizer (receives all previous results)
        try:
            results["synthesis"] = run_synthesizer(
                engine=self.engine,
                categorization=results.get("categorization"),
                weekly=results.get("weekly_analysis"),
                monthly=results.get("monthly_analysis"),
                anomaly=results.get("anomaly"),
            )
        except Exception as exc:
            logger.error("Synthesizer agent failed: %s", exc)
            errors.append(f"synthesis: {exc}")

        # Finish run
        error_msg = "; ".join(errors) if errors else None
        status = RunStatus.FAILED if not results else RunStatus.SUCCEEDED
        self._finish_run(run_id, results=results, error=error_msg, status=status)

        return run_id

    def run_single(self, agent_type: str) -> str:
        """Run a single agent, return the SyncRun ID."""
        if agent_type not in VALID_AGENT_TYPES:
            raise ValueError(
                f"Invalid agent type '{agent_type}'. "
                f"Valid: {', '.join(sorted(VALID_AGENT_TYPES))}"
            )

        run_id = str(uuid.uuid4())
        self._create_run(run_id, agent_type=agent_type)

        try:
            result = self._run_agent(agent_type)
            self._finish_run(run_id, results={agent_type: result})
        except Exception as exc:
            logger.error("Agent %s failed: %s", agent_type, exc)
            self._finish_run(run_id, results={}, error=str(exc), status=RunStatus.FAILED)

        return run_id

    def _run_agent(self, agent_type: str) -> Any:
        """Dispatch to the correct agent runner."""
        if agent_type == "categorization":
            return run_categorization(self.engine)
        elif agent_type == "weekly_analysis":
            return run_weekly_analysis(self.engine)
        elif agent_type == "monthly_analysis":
            return run_monthly_analysis(self.engine)
        elif agent_type == "anomaly":
            return run_anomaly_detection(self.engine)
        elif agent_type == "synthesis":
            return run_synthesizer(engine=self.engine)
        raise ValueError(f"Unknown agent type: {agent_type}")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _create_run(self, run_id: str, agent_type: str) -> None:
        with Session(self.engine) as session:
            session.add(
                AgentRun(
                    id=run_id,
                    agent_name=agent_type,
                    status=RunStatus.RUNNING,
                    trigger=RunTrigger.MANUAL,
                )
            )
            session.commit()



    def _finish_run(
        self,
        run_id: str,
        *,
        results: dict[str, Any],
        error: str | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
    ) -> None:
        # Convert Pydantic models to dicts
        json_results = {}
        for k, v in results.items():
            json_results[k] = v.model_dump() if hasattr(v, "model_dump") else v

        with Session(self.engine) as session:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(timezone.utc),
                    result=json_results,
                    error=error,
                )
            )
            session.commit()
