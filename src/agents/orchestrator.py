"""Agent Orchestrator — sequences agents and persists results.

Follows the same SyncRun lifecycle pattern as NormalizationPipeline:
create run → execute → persist results → finish run.
"""

from __future__ import annotations

import calendar
import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Engine, create_engine, delete, select, update
from sqlalchemy.orm import Session

from src.agents import (
    anomaly as anomaly_module,
    categorization as categorization_module,
    monthly_analysis as monthly_module,
    synthesizer as synthesizer_module,
    weekly_analysis as weekly_module,
)
from src.agents._usage import AgentUsage
from src.agents.anomaly import run_anomaly_detection
from src.agents.categorization import (
    DEFAULT_AUTO_APPLY_CONFIDENCE,
    run_categorization,
)
from src.agents.monthly_analysis import run_monthly_analysis
from src.agents.reaper import RunCancelled
from src.agents.synthesizer import run_synthesizer
from src.agents.weekly_analysis import run_weekly_analysis
from src.core.db.models import (
    AgentRun,
    AppSettings,
    Report,
    ReportStatus,
    RunStatus,
    RunTrigger,
)

# Map agent_type → model identifier for usage_detail serialisation.
AGENT_MODELS: dict[str, str] = {
    "categorization": categorization_module.MODEL,
    "weekly_analysis": weekly_module.MODEL,
    "monthly_analysis": monthly_module.MODEL,
    "anomaly": anomaly_module.MODEL,
    "synthesis": synthesizer_module.MODEL,
}


def _usage_to_dict(usage: AgentUsage, model: str) -> dict[str, Any]:
    """Serialise a single-agent AgentUsage for JSON persistence.

    Per-agent extras (e.g. categorization's `memory` block) are folded
    into the same dict so they show up under `usage_detail.<agent>.<key>`.
    """
    out: dict[str, Any] = {
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": str(usage.cost_usd),
    }
    if usage.extra:
        out.update(usage.extra)
    return out

logger = logging.getLogger(__name__)

def _classify_error(msg: str) -> str:
    """Translate raw exception messages into something a user can act on."""
    lower = msg.lower()
    if "specified api usage limits" in lower or (
        "invalid_request_error" in lower and "usage limit" in lower
    ):
        return (
            "Anthropic API-Limit erreicht — Spending-Cap in der Console "
            "(console.anthropic.com → Settings → Limits) erhöhen."
        )
    if "rate_limit" in lower or "429" in msg:
        return "Anthropic Rate-Limit getroffen — bitte in 1–2 Min nochmal."
    if "connection error" in lower or "connect" in lower:
        return "Verbindung zur Anthropic API gescheitert — Netzwerk prüfen."
    if "exceeded maximum retries" in lower:
        return "LLM hat das Output-Schema mehrfach verletzt — Prompt/Modell prüfen."
    return msg


def _format_errors(errors: list[str]) -> str:
    """Group identical errors and run them through the classifier.

    Many of our errors come from the same root cause (all LLM calls hitting
    the same quota) — collapse duplicates so the user sees one clean line
    instead of a wall of identical tracebacks.
    """
    classified: dict[str, list[str]] = {}
    for raw in errors:
        # Each entry is "<agent_name>: <message>" — split off the agent.
        agent, _, body = raw.partition(": ")
        message = _classify_error(body or raw)
        classified.setdefault(message, []).append(agent or "?")
    parts = []
    for message, agents in classified.items():
        if len(agents) == 1:
            parts.append(f"{agents[0]}: {message}")
        else:
            parts.append(f"{', '.join(agents)}: {message}")
    return " | ".join(parts)


_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")
_ISO_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_period(period: str | None, fallback: date) -> tuple[date, date]:
    """Parse an agent's `result.period` string into a (start, end) date pair.

    Supported formats:
      - None / empty                → (fallback, fallback)
      - "2026-W16"                  → ISO week → Mon..Sun
      - "2026-03"                   → first..last day of month
      - "2026-03-19/2026-04-18"     → split on '/', both ISO dates
      - anything else               → (fallback, fallback)
    """
    if not period:
        return fallback, fallback

    if "/" in period:
        try:
            start_str, end_str = period.split("/", 1)
            if _ISO_DATE_RE.match(start_str) and _ISO_DATE_RE.match(end_str):
                return date.fromisoformat(start_str), date.fromisoformat(end_str)
        except ValueError:
            pass

    m = _ISO_WEEK_RE.match(period)
    if m:
        try:
            year, week = int(m.group(1)), int(m.group(2))
            monday = date.fromisocalendar(year, week, 1)
            sunday = date.fromisocalendar(year, week, 7)
            return monday, sunday
        except ValueError:
            pass

    m = _ISO_MONTH_RE.match(period)
    if m:
        try:
            year, month = int(m.group(1)), int(m.group(2))
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, 1), date(year, month, last_day)
        except (ValueError, calendar.IllegalMonthError):
            pass

    return fallback, fallback


VALID_AGENT_TYPES = frozenset(
    {
        "categorization",
        "weekly_analysis",
        "monthly_analysis",
        "anomaly",
        "synthesis",
    }
)


class AgentOrchestrator:
    def __init__(self, database_url: str, own_ibans: list[str] | None = None):
        self.engine: Engine = create_engine(database_url)
        self.own_ibans = list(own_ibans or [])

    # ------------------------------------------------------------------
    # Entry points called from the Runs API (run ID already exists)
    # ------------------------------------------------------------------

    def run_single_for(self, run_id: str, agent_type: str) -> None:
        """Execute a single agent for an existing run (API-triggered)."""
        self._mark_running(run_id)
        try:
            result, usage = self._run_agent(agent_type, run_id=run_id)
            results = {agent_type: result}
            detail = {agent_type: _usage_to_dict(usage, AGENT_MODELS.get(agent_type, "unknown"))} if usage.input_tokens or usage.output_tokens else None
            self._finish_run(run_id, results=results, usage=usage, usage_detail=detail)
            self._persist_report(agent_type, result)
        except RunCancelled as exc:
            # Row already reflects the truth (cancelled by user / reaped).
            # Don't overwrite — finalise nothing.
            logger.info("Run %s stopped cooperatively: %s", run_id, exc)
        except Exception as exc:
            logger.error("Agent %s failed: %s", agent_type, exc)
            self._finish_run(
                run_id, results={}, error=str(exc), status=RunStatus.FAILED
            )

    def run_full_for(self, run_id: str) -> None:
        """Execute all agents for an existing run (API-triggered)."""
        self._mark_running(run_id)
        try:
            results, errors, usage, detail = self._run_all_agents(run_id=run_id)
        except RunCancelled as exc:
            logger.info("Run %s stopped cooperatively: %s", run_id, exc)
            return
        error_msg = _format_errors(errors) if errors else None
        # Any agent failure makes the whole pipeline failed — partial
        # success would be misleading (the synthesis agent emits an empty
        # result when its inputs are None, which used to mask total failure).
        status = RunStatus.FAILED if errors else RunStatus.SUCCEEDED
        self._finish_run(run_id, results=results, error=error_msg, status=status, usage=usage, usage_detail=detail)

    # ------------------------------------------------------------------
    # Standalone entry points (create their own run)
    # ------------------------------------------------------------------

    def run_full(self) -> str:
        """Run all agents in sequence, return the AgentRun ID."""
        run_id = str(uuid.uuid4())
        self._create_run(run_id, agent_type="full")
        results, errors, usage, detail = self._run_all_agents(run_id=run_id)
        error_msg = _format_errors(errors) if errors else None
        status = RunStatus.FAILED if errors else RunStatus.SUCCEEDED
        self._finish_run(run_id, results=results, error=error_msg, status=status, usage=usage, usage_detail=detail)
        return run_id

    def run_single(self, agent_type: str) -> str:
        """Run a single agent, return the AgentRun ID."""
        if agent_type not in VALID_AGENT_TYPES:
            raise ValueError(
                f"Invalid agent type '{agent_type}'. "
                f"Valid: {', '.join(sorted(VALID_AGENT_TYPES))}"
            )

        run_id = str(uuid.uuid4())
        self._create_run(run_id, agent_type=agent_type)

        try:
            result, usage = self._run_agent(agent_type, run_id=run_id)
            detail = {agent_type: _usage_to_dict(usage, AGENT_MODELS.get(agent_type, "unknown"))} if usage.input_tokens or usage.output_tokens else None
            self._finish_run(run_id, results={agent_type: result}, usage=usage, usage_detail=detail)
            self._persist_report(agent_type, result)
        except Exception as exc:
            logger.error("Agent %s failed: %s", agent_type, exc)
            self._finish_run(
                run_id, results={}, error=str(exc), status=RunStatus.FAILED
            )

        return run_id

    # ------------------------------------------------------------------
    # Agent dispatch
    # ------------------------------------------------------------------

    _PIPELINE_STEPS = [
        ("categorization", "Kategorisierung"),
        ("weekly_analysis", "Wochenanalyse"),
        ("monthly_analysis", "Monatsanalyse"),
        ("anomaly", "Anomalie-Erkennung"),
        ("synthesis", "Synthese"),
    ]

    def _run_all_agents(
        self, run_id: str | None = None
    ) -> tuple[dict[str, Any], list[str], AgentUsage, dict[str, dict[str, Any]]]:
        """Run all agents, return (results, errors, total_usage, per_agent_detail).

        `per_agent_detail` maps agent_type → {model, input_tokens, output_tokens, cost_usd}
        — each agent gets its own `AgentUsage` so the orchestrator can show a
        breakdown in the UI. Totals are the sum across all entries.
        """
        results: dict[str, Any] = {}
        errors: list[str] = []
        total_usage = AgentUsage()
        per_agent: dict[str, AgentUsage] = {}
        threshold = self._load_auto_apply_threshold()
        total_steps = len(self._PIPELINE_STEPS)

        for idx, (agent_type, label) in enumerate(self._PIPELINE_STEPS):
            if agent_type == "synthesis":
                continue
            if run_id:
                self._update_progress(
                    run_id,
                    current=idx,
                    total=total_steps,
                    message=f"Agent {idx + 1}/{total_steps}: {label}",
                )
            agent_usage = AgentUsage()
            try:
                if agent_type == "categorization":
                    cb = self._make_progress_callback(run_id, label) if run_id else None
                    err_cb = self._make_batch_error_callback(run_id) if run_id else None
                    result = run_categorization(
                        self.engine,
                        on_progress=cb,
                        on_batch_error=err_cb,
                        auto_apply_threshold=threshold,
                        usage=agent_usage,
                    )
                elif agent_type == "weekly_analysis":
                    result = run_weekly_analysis(self.engine, usage=agent_usage)
                elif agent_type == "monthly_analysis":
                    result = run_monthly_analysis(self.engine, usage=agent_usage)
                elif agent_type == "anomaly":
                    result = run_anomaly_detection(self.engine, usage=agent_usage)
                else:
                    raise ValueError(f"Unhandled agent_type {agent_type}")
                results[agent_type] = result
                self._persist_report(agent_type, result)
            except RunCancelled:
                # Bubble up to run_full_for / run_single_for — don't swallow
                # cancellation as a per-agent error.
                per_agent[agent_type] = agent_usage
                total_usage.merge(agent_usage)
                raise
            except Exception as exc:
                logger.error("%s agent failed: %s", agent_type, exc)
                errors.append(f"{agent_type}: {exc}")
            finally:
                per_agent[agent_type] = agent_usage
                total_usage.merge(agent_usage)

        # Synthesizer receives all prior results
        if run_id:
            self._update_progress(
                run_id,
                current=total_steps - 1,
                total=total_steps,
                message=f"Agent {total_steps}/{total_steps}: Synthese",
            )
        synth_usage = AgentUsage()
        try:
            result = run_synthesizer(
                engine=self.engine,
                categorization=results.get("categorization"),
                weekly=results.get("weekly_analysis"),
                monthly=results.get("monthly_analysis"),
                anomaly=results.get("anomaly"),
                usage=synth_usage,
            )
            results["synthesis"] = result
            self._persist_report("synthesis", result)
        except RunCancelled:
            per_agent["synthesis"] = synth_usage
            total_usage.merge(synth_usage)
            raise
        except Exception as exc:
            logger.error("Synthesizer agent failed: %s", exc)
            errors.append(f"synthesis: {exc}")
        finally:
            per_agent["synthesis"] = synth_usage
            total_usage.merge(synth_usage)

        if run_id:
            self._update_progress(
                run_id,
                current=total_steps,
                total=total_steps,
                message="Pipeline abgeschlossen",
            )

        logger.info(
            "Pipeline usage: %d input + %d output tokens (~$%s)",
            total_usage.input_tokens,
            total_usage.output_tokens,
            total_usage.cost_usd,
        )

        # Serialise per-agent usage for DB persistence — skip agents that made
        # zero calls (empty records just add noise in the UI).
        per_agent_detail = {
            agent_type: _usage_to_dict(u, AGENT_MODELS.get(agent_type, "unknown"))
            for agent_type, u in per_agent.items()
            if u.input_tokens or u.output_tokens
        }
        return results, errors, total_usage, per_agent_detail

    def _run_agent(
        self, agent_type: str, run_id: str | None = None
    ) -> tuple[Any, AgentUsage]:
        """Dispatch to the correct agent runner, return (result, usage)."""
        usage = AgentUsage()
        if agent_type == "categorization":
            cb = (
                self._make_progress_callback(run_id, "Kategorisierung")
                if run_id
                else None
            )
            result = run_categorization(
                self.engine,
                on_progress=cb,
                auto_apply_threshold=self._load_auto_apply_threshold(),
                usage=usage,
            )
            return result, usage
        if run_id:
            label = next(
                (lbl for at, lbl in self._PIPELINE_STEPS if at == agent_type), agent_type
            )
            self._update_progress(run_id, message=f"{label} läuft…")
        if agent_type == "weekly_analysis":
            return run_weekly_analysis(self.engine, usage=usage), usage
        elif agent_type == "monthly_analysis":
            return run_monthly_analysis(self.engine, usage=usage), usage
        elif agent_type == "anomaly":
            return run_anomaly_detection(self.engine, usage=usage), usage
        elif agent_type == "synthesis":
            return run_synthesizer(engine=self.engine, usage=usage), usage
        raise ValueError(f"Unknown agent type: {agent_type}")

    def _make_progress_callback(
        self, run_id: str, prefix: str
    ):
        """Build a closure that pipes (current, total, message) into the DB."""

        def _cb(current: int, total: int, message: str) -> None:
            self._update_progress(
                run_id,
                current=current,
                total=total,
                message=f"{prefix}: {message}",
            )

        return _cb

    def _make_batch_error_callback(self, run_id: str):
        """Build a closure that writes/clears `last_error` on the run row."""

        def _cb(message: str | None) -> None:
            self._record_last_error(run_id, message)

        return _cb

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _update_progress(
        self,
        run_id: str,
        *,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
    ) -> None:
        """Write live progress fields + heartbeat, then assert run is still RUNNING.

        Best-effort on the DB write: a hiccup never aborts the run. The
        post-write status check raises RunCancelled if the row is no longer
        RUNNING — set by the user (`cancelled`) or the reaper (`failed`).
        Cooperative cancellation point — kept here because every batch
        boundary already calls _update_progress.
        """
        values: dict[str, Any] = {"heartbeat_at": datetime.now(timezone.utc)}
        if current is not None:
            values["progress_current"] = current
        if total is not None:
            values["progress_total"] = total
        if message is not None:
            values["progress_message"] = message

        current_status: RunStatus | None = None
        try:
            with Session(self.engine) as session:
                session.execute(
                    update(AgentRun)
                    .where(AgentRun.id == run_id)
                    .values(**values)
                )
                current_status = session.execute(
                    select(AgentRun.status).where(AgentRun.id == run_id)
                ).scalar_one_or_none()
                session.commit()
        except Exception:
            logger.exception("Failed to update progress for run %s", run_id)
            return

        if current_status is not None and current_status != RunStatus.RUNNING:
            raise RunCancelled(
                f"Run {run_id} left RUNNING (now {current_status.value}); "
                "stopping cooperative cancellation"
            )

    def _record_last_error(self, run_id: str, message: str | None) -> None:
        """Write a transient error visible while status='running'.

        Pass `None` (or empty string) to clear it after a recovery. The
        persistent `error` column is set separately by `_finish_run`.
        """
        clean = (message or None) and message[:500]
        try:
            with Session(self.engine) as session:
                session.execute(
                    update(AgentRun)
                    .where(AgentRun.id == run_id)
                    .values(last_error=clean)
                )
                session.commit()
        except Exception:
            logger.exception("Failed to record last_error for run %s", run_id)

    def _load_auto_apply_threshold(self) -> float:
        """Read the user-configured auto-apply threshold from app_settings.

        Falls back to the module default if the singleton row is missing
        (fresh DB without the seed insert).
        """
        with Session(self.engine) as session:
            row = session.get(AppSettings, 1)
            if row is None:
                return DEFAULT_AUTO_APPLY_CONFIDENCE
            return float(row.auto_apply_confidence)

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

    def _mark_running(self, run_id: str) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(status=RunStatus.RUNNING)
            )
            session.commit()

    def _finish_run(
        self,
        run_id: str,
        *,
        results: dict[str, Any],
        error: str | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
        usage: AgentUsage | None = None,
        usage_detail: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        json_results = {}
        for k, v in results.items():
            json_results[k] = v.model_dump() if hasattr(v, "model_dump") else v

        values: dict[str, Any] = {
            "status": status,
            "finished_at": datetime.now(timezone.utc),
            "result": json_results,
            "error": error,
            # Final state replaces the transient running-state warning.
            "last_error": None,
        }
        if usage is not None:
            values["input_tokens"] = usage.input_tokens
            values["output_tokens"] = usage.output_tokens
            values["cost_usd"] = usage.cost_usd
        if usage_detail is not None:
            values["usage_detail"] = usage_detail

        with Session(self.engine) as session:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(**values)
            )
            session.commit()

    def _persist_report(self, agent_type: str, result: Any) -> None:
        """Save an agent result as a Report row (agent memory loop).

        Replaces any existing report with the same (report_type, period) so
        re-runs don't accumulate duplicates in the UI.
        """
        today = date.today()
        content = result.model_dump() if hasattr(result, "model_dump") else result
        period_str = getattr(result, "period", None)
        period_start, period_end = _parse_period(period_str, today)
        title = f"{agent_type} — {period_str or today.isoformat()}"

        with Session(self.engine) as session:
            session.execute(
                delete(Report)
                .where(Report.report_type == agent_type)
                .where(Report.period_start == period_start)
                .where(Report.period_end == period_end)
            )
            session.add(
                Report(
                    id=uuid.uuid4().hex,
                    report_type=agent_type,
                    title=title,
                    content=content,
                    period_start=period_start,
                    period_end=period_end,
                    format="json",
                    status=ReportStatus.READY,
                )
            )
            session.commit()

