"""comdirect-worker — internal sync service.

Runs on port 8001, never exposed to the internet.
Receives sync requests from the public comdirect-api service.
"""

import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.agents.reaper import reap_stale_runs, start_periodic_reaper
from src.api.routers import (
    aggregates,
    auth,
    categories,
    categorization,
    reports,
    runs,
    settings as settings_router,
    transactions,
)
from src.connector.comdirect_client import ComdirectClient
from src.connector.yfinance_client import (
    CurrencyMismatchError,
    HistoryProvider,
    PriceFetchError,
    YFinanceClient,
)
from src.core.config import settings
from src.core.db.models import Instrument, InstrumentPriceHistory
from src.core.logging import get_logger, setup_logging
from src.core.notifier import notify_failure_from_db

setup_logging()
logger = get_logger("worker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot-time reaper + periodic stale-run reaper.

    The boot-mode reaper sweeps `RUNNING` rows orphaned by a previous
    worker death (heartbeat_at NULL or older than the cutoff). The
    periodic loop catches in-flight orphans for the rest of the
    process's lifetime — every 5 min, very cheap.
    """
    engine = create_engine(settings.database_url)
    try:
        reaped = await asyncio.to_thread(reap_stale_runs, engine, boot_mode=True)
        if reaped:
            logger.warning("Boot reaper: marked %d stale run(s) as failed", reaped)
    except Exception:
        # Don't refuse to start the worker — but we will log loudly.
        logger.exception("Boot reaper failed; serving anyway")

    reaper_task = await start_periodic_reaper(engine)
    app.state.engine = engine
    app.state.reaper_task = reaper_task
    try:
        yield
    finally:
        reaper_task.cancel()
        try:
            await reaper_task
        except (asyncio.CancelledError, Exception):
            pass
        engine.dispose()


app = FastAPI(
    title="k-fin Worker",
    description="Internal sync worker — not publicly accessible",
    version="0.2.0",
    lifespan=lifespan,
)

_origins = settings.get_cors_origins()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ---------------------------------------------------------------------------
# In-memory pending sessions (single-worker deployment)
# ---------------------------------------------------------------------------

_pending_sessions: dict[str, dict] = {}

# Finance API (M6) — transaction, category & aggregate endpoints
app.include_router(auth.router, prefix="/api/v1")
app.include_router(transactions.router, prefix="/api/v1")
app.include_router(categories.router, prefix="/api/v1")
app.include_router(aggregates.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")
app.include_router(settings_router.router, prefix="/api/v1")
app.include_router(categorization.router, prefix="/api/v1")


class SyncStartRequest(BaseModel):
    account_transaction_limit: int | None = Field(default=None, gt=0)
    account_transaction_min_booking_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$|^-\d+d$"
    )
    depot_transaction_limit: int | None = Field(default=None, gt=0)
    depot_transaction_min_booking_date: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$|^-\d+d$"
    )

    @field_validator(
        "account_transaction_min_booking_date",
        "depot_transaction_min_booking_date",
    )
    @classmethod
    def validate_relative_date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        m = re.match(r"^-(\d+)d$", v)
        if m and int(m.group(1)) > 3650:
            raise ValueError("Relative date must not exceed 3650 days (~10 years)")
        return v


@app.get("/health")
def health():
    return {"status": "ok", "service": "comdirect-worker", "platform": "k-fin"}


class RunDispatchRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    # Optional override for the agent's built-in time window. Forwarded to
    # weekly/monthly/anomaly agents; categorization + synthesis ignore it.
    period_days: int | None = Field(default=None, ge=1, le=3650)


def _execute_run_in_worker(
    run_id: str, agent_name: str, period_days: int | None = None
) -> None:
    """Background body: dispatch the run via the orchestrator.

    Imported lazily so the worker boot doesn't pay the heavy
    pydantic_ai import cost until the first run lands.
    """
    from src.agents.orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator(database_url=settings.database_url)
    orchestrator.run_single_for(run_id, agent_name, period_days=period_days)


def _execute_full_run_in_worker(
    run_id: str, period_days: int | None = None
) -> None:
    from src.agents.orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator(database_url=settings.database_url)
    orchestrator.run_full_for(run_id, period_days=period_days)


@app.post("/internal/runs/start", status_code=202)
def internal_run_start(
    agent_name: str,
    payload: RunDispatchRequest,
    background_tasks: BackgroundTasks,
):
    """Dispatch a single-agent run for an existing AgentRun row.

    Fire-and-forget from the api caller's perspective. The row is created
    (status=pending) by the api before this is invoked. The orchestrator
    flips it to running, drives heartbeats, and writes the terminal status.
    """
    background_tasks.add_task(
        _execute_run_in_worker, payload.run_id, agent_name, payload.period_days
    )
    return {"status": "accepted", "run_id": payload.run_id, "agent": agent_name}


@app.post("/internal/runs/start-full", status_code=202)
def internal_run_start_full(
    payload: RunDispatchRequest,
    background_tasks: BackgroundTasks,
):
    """Dispatch a full-pipeline run (all agents, sequentially)."""
    background_tasks.add_task(
        _execute_full_run_in_worker, payload.run_id, payload.period_days
    )
    return {"status": "accepted", "run_id": payload.run_id, "agent": "full"}


# ---------------------------------------------------------------------------
# Portfolio price backfill (yfinance) — runs in the worker so the
# public-facing `api` pod has zero outbound to Yahoo Finance and zero
# direct DB writes for this code path. The api router is a thin
# dispatcher that forwards to /internal/portfolio/backfill-prices.
# ---------------------------------------------------------------------------


def _get_history_provider() -> HistoryProvider:
    """FastAPI dependency for the historical-price provider.

    Mirrors the api-side override hook so worker tests can swap in a
    fake provider via `app.dependency_overrides`.
    """
    return YFinanceClient()


class _InternalBackfillRequest(BaseModel):
    isin: str = Field(min_length=1, max_length=32)
    from_date: date
    to_date: date


class _InternalBackfillResult(BaseModel):
    isin: str
    ticker_symbol: str
    requested_from: date
    requested_to: date
    fetched_points: int
    inserted_points: int
    skipped_existing: int
    source: str = "yfinance"


@app.post(
    "/internal/portfolio/backfill-prices",
    response_model=_InternalBackfillResult,
)
def internal_portfolio_backfill_prices(
    payload: _InternalBackfillRequest,
    provider: HistoryProvider = Depends(_get_history_provider),
):
    """Fetch missing daily close prices for an instrument from yfinance.

    Internal-only — the api dispatcher (`POST /portfolio/instruments/{isin}/
    backfill-prices`) has already validated auth and basic input shape.
    Runs the yfinance call + `instrument_price_history` upsert here so
    the public api never makes outbound to Yahoo Finance and the api
    NetworkPolicy can stay tight.
    """
    if payload.from_date > payload.to_date:
        raise HTTPException(
            status_code=400, detail="from_date must be <= to_date"
        )

    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as db:
            instrument = db.get(Instrument, payload.isin)
            if instrument is None:
                raise HTTPException(
                    status_code=404, detail="Instrument not found"
                )
            if not instrument.ticker_symbol:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Instrument has no ticker_symbol; PATCH it first via "
                        "/portfolio/instruments/{isin}"
                    ),
                )

            try:
                points = provider.get_history(
                    instrument.ticker_symbol,
                    payload.from_date,
                    payload.to_date,
                    expected_currency=instrument.currency,
                )
            except CurrencyMismatchError as exc:
                logger.warning(
                    "backfill-prices(%s, ticker=%s) currency mismatch: %s",
                    payload.isin,
                    instrument.ticker_symbol,
                    exc,
                )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except PriceFetchError as exc:
                logger.warning(
                    "backfill-prices(%s, ticker=%s) provider error: %s",
                    payload.isin,
                    instrument.ticker_symbol,
                    exc,
                )
                raise HTTPException(
                    status_code=502,
                    detail="Upstream price provider failed",
                ) from exc

            if not points:
                return _InternalBackfillResult(
                    isin=payload.isin,
                    ticker_symbol=instrument.ticker_symbol,
                    requested_from=payload.from_date,
                    requested_to=payload.to_date,
                    fetched_points=0,
                    inserted_points=0,
                    skipped_existing=0,
                )

            existing_dates = set(
                db.execute(
                    select(InstrumentPriceHistory.price_date).where(
                        InstrumentPriceHistory.isin == payload.isin,
                        InstrumentPriceHistory.price_date >= payload.from_date,
                        InstrumentPriceHistory.price_date <= payload.to_date,
                    )
                )
                .scalars()
                .all()
            )

            inserted = 0
            skipped = 0
            for point in points:
                if point.price_date in existing_dates:
                    skipped += 1
                    continue
                db.add(
                    InstrumentPriceHistory(
                        isin=payload.isin,
                        price_date=point.price_date,
                        close=point.close,
                        currency=point.currency or instrument.currency,
                        source="yfinance",
                    )
                )
                existing_dates.add(point.price_date)
                inserted += 1
            db.commit()

            return _InternalBackfillResult(
                isin=payload.isin,
                ticker_symbol=instrument.ticker_symbol,
                requested_from=payload.from_date,
                requested_to=payload.to_date,
                fetched_points=len(points),
                inserted_points=inserted,
                skipped_existing=skipped,
            )
    finally:
        engine.dispose()


@app.post("/internal/normalize")
def internal_normalize():
    """Re-run normalization pipeline over existing raw_transactions."""
    from src.normalization.pipeline import NormalizationPipeline

    pipeline = NormalizationPipeline(
        database_url=settings.database_url,
        own_ibans=settings.get_own_ibans(),
    )
    df, run_id = pipeline.process_and_normalize()
    return {"status": "done", "normalized": len(df), "run_id": run_id}


@app.post("/internal/sync/start")
async def internal_sync_start(payload: SyncStartRequest | None = None):
    """Step 1: Begin auth flow and trigger TAN challenge."""
    client = ComdirectClient()

    try:
        auth_state = await client.begin_auth()
    except RuntimeError as exc:
        logger.error(f"begin_auth failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))

    session_id = str(uuid.uuid4())
    _pending_sessions[session_id] = {
        "client": client,
        "session_identifier": auth_state["session_identifier"],
        "challenge_id": auth_state["challenge_id"],
        "config": (payload.model_dump(exclude_none=True) if payload else {}),
    }

    logger.info(f"TAN challenge sent, session_id={session_id}")
    return {"status": "pending_tan", "session_id": session_id}


@app.post("/internal/sync/confirm")
async def internal_sync_confirm(session_id: str):
    """Step 2: Complete auth after TAN confirmation and run export."""
    if session_id not in _pending_sessions:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    pending = _pending_sessions.pop(session_id)
    client: ComdirectClient = pending["client"]
    config = pending.get("config", {})

    ok = await client.complete_auth(
        pending["session_identifier"],
        pending["challenge_id"],
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Session activation failed")

    # Run export using the authenticated client
    try:
        from pathlib import Path

        from scripts.export_csv import (
            export_account_to_csv,
            export_depot_positions_csv,
            export_depot_transactions_csv,
            export_summary_csv,
        )

        output_dir = Path("/data/exports")
        output_dir.mkdir(parents=True, exist_ok=True)

        data = await client.get_all_data(
            account_transaction_limit=config.get(
                "account_transaction_limit", settings.account_transaction_limit
            ),
            account_transaction_min_booking_date=config.get(
                "account_transaction_min_booking_date",
                settings.account_transaction_min_booking_date,
            ),
            depot_transaction_limit=config.get(
                "depot_transaction_limit", settings.depot_transaction_limit
            ),
            depot_transaction_min_booking_date=config.get(
                "depot_transaction_min_booking_date",
                settings.depot_transaction_min_booking_date,
            ),
        )

        # CSV exports
        for acc in data["accounts"]:
            account_inner = acc.get("account") or acc
            account_id = account_inner.get("accountId")
            iban = account_inner.get("iban", account_id)
            if not account_id:
                continue
            balance_obj = acc.get("balance") or {}
            acc_type_obj = account_inner.get("accountType") or {}
            acc_type_text = (
                acc_type_obj.get("text", "Girokonto")
                if isinstance(acc_type_obj, dict)
                else str(acc_type_obj)
            )
            balance_info = {
                "value": (
                    balance_obj.get("value", "0")
                    if isinstance(balance_obj, dict)
                    else str(balance_obj)
                ),
                "unit": (
                    balance_obj.get("unit", "EUR") if isinstance(balance_obj, dict) else "EUR"
                ),
                "account_type": acc_type_text,
            }
            txs = data["transactions"].get(account_id, [])
            export_account_to_csv(account_id, iban, balance_info, txs, output_dir)

        all_positions: list[dict] = []
        for depot in data["depots"]:
            depot_id = depot.get("depotId")
            if not depot_id:
                continue
            positions = data["depot_positions"].get(depot_id, [])
            all_positions.extend(positions)
            export_depot_positions_csv(depot_id, positions, output_dir)
            depot_txs = data["depot_transactions"].get(depot_id, [])
            export_depot_transactions_csv(depot_id, depot_txs, output_dir)

        export_summary_csv(data["accounts"], all_positions, output_dir)

        # JSON export
        import json as json_mod

        from src.exporter.json_export import build_export

        payload = build_export(data)
        from datetime import date

        json_path = output_dir / f"comdirect_export_{date.today().isoformat()}.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json_mod.dump(payload, fh, ensure_ascii=False, indent=2)

        # ----------------------------------------------------------
        # Ingest raw transactions + normalize (best-effort)
        # ----------------------------------------------------------
        ingest_result = None
        try:
            from datetime import date as _date

            from sqlalchemy.orm import Session

            from src.normalization.depot_ingest import capture_snapshot, ingest_depots
            from src.normalization.ingest import ingest_json_export
            from src.normalization.pipeline import NormalizationPipeline

            pipeline = NormalizationPipeline(
                database_url=settings.database_url,
                own_ibans=settings.get_own_ibans(),
            )
            inserted = ingest_json_export(pipeline, json_path)
            logger.info("Ingested %d raw transactions", inserted)

            _df, _normalize_run_id = pipeline.process_and_normalize()
            logger.info("Normalization completed (%d rows)", len(_df))

            depot_stats: dict[str, int] | None = None
            try:
                with Session(pipeline.engine) as depot_session:
                    depot_stats = ingest_depots(depot_session, data)
                    capture_snapshot(depot_session, _date.today())
                logger.info("Depot ingest: %s", depot_stats)
            except Exception:
                logger.exception("Depot ingest failed (transactions ingest still succeeded)")

            ingest_result = {
                "inserted": inserted,
                "normalized": len(_df),
                "normalize_run_id": _normalize_run_id,
                "depots": depot_stats,
            }
        except Exception:
            logger.exception("Ingest/normalization failed (export still succeeded)")

        # Agent pipeline is intentionally NOT triggered here — sync only
        # fetches Comdirect data + normalizes. Trigger LLM agents
        # separately via POST /api/v1/runs/full so the user controls
        # cost/time and the long-running phase shows up in the dedicated
        # Agents UI with progress.

        logger.info("Export completed successfully")
    except Exception as exc:
        logger.exception("Export failed")
        # Best-effort failure ping. The HTTP 500 below is the source of
        # truth for the api caller; the webhook is informational. We
        # surface ``session_id`` as the run-ID since this code path
        # doesn't create a SyncRun row of its own.
        _engine = create_engine(settings.database_url)
        try:
            notify_failure_from_db(
                _engine,
                run_kind="sync",
                run_id=session_id,
                error_message=f"Export failed: {exc}",
            )
        finally:
            _engine.dispose()
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

    return {
        "status": "done",
        "message": "Sync abgeschlossen — Agents separat starten",
        "ingest": ingest_result,
    }


# ---------------------------------------------------------------------------
# Historical backfill endpoints (M-backfill)
# ---------------------------------------------------------------------------


class BackfillStartRequest(BaseModel):
    months: int = Field(default=24, ge=1, le=24)


class BackfillStartResponse(BaseModel):
    status: str
    session_id: str


class BackfillRunResponse(BaseModel):
    run_id: str
    status: str
    target_start_date: str
    current_window_start: str | None
    windows_total: int
    windows_done: int
    rows_inserted: int
    progress_message: str | None
    error: str | None
    started_at: str
    finished_at: str | None


def _execute_backfill_in_worker(
    run_id: str,
    client: ComdirectClient,
    target_start_date_iso: str,
) -> None:
    """Background body: drive the backfill walker and write terminal status.

    Runs in a worker BackgroundTask so the HTTP confirm-call returns
    immediately. Status updates land on the BackfillRun row continuously.
    """
    import asyncio as _asyncio
    from datetime import date as _date

    from src.core.db.models import BackfillStatus
    from src.normalization.pipeline import NormalizationPipeline
    from src.scheduler.backfill import (
        mark_run_terminal,
        run_backfill,
    )

    pipeline = NormalizationPipeline(
        database_url=settings.database_url,
        own_ibans=settings.get_own_ibans(),
    )

    async def _drive() -> None:
        try:
            accounts = await client.get_accounts()
            account_ids: list[str] = []
            for acc in accounts:
                inner = acc.get("account") or acc
                aid = inner.get("accountId")
                if aid:
                    account_ids.append(aid)

            if not account_ids:
                raise RuntimeError("No accounts returned from Comdirect")

            target = _date.fromisoformat(target_start_date_iso)
            await run_backfill(
                client=client,
                pipeline=pipeline,
                run_id=run_id,
                target_start_date=target,
                account_ids=account_ids,
            )
            mark_run_terminal(pipeline, run_id, status=BackfillStatus.SUCCEEDED)
            logger.info("Backfill %s succeeded", run_id)
        except Exception as exc:
            logger.exception("Backfill %s failed", run_id)
            mark_run_terminal(
                pipeline, run_id, status=BackfillStatus.FAILED, error=str(exc)
            )
            # Best-effort: the terminal status write above already
            # made the failure visible in the UI. Webhook is a courtesy.
            notify_failure_from_db(
                pipeline.engine,
                run_kind="backfill",
                run_id=run_id,
                error_message=str(exc),
            )

    _asyncio.run(_drive())


@app.post("/internal/backfill/start", response_model=BackfillStartResponse)
async def internal_backfill_start(payload: BackfillStartRequest | None = None):
    """Step 1: Begin backfill auth flow and trigger TAN challenge.

    Refuses (409) if any backfill is already running — max one per user.
    """
    from sqlalchemy.orm import Session

    from src.core.db.models import BackfillRun, BackfillStatus

    config = payload.model_dump() if payload else {"months": 24}

    # Concurrency guard: refuse if a run is already in-flight.
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as s:
            existing = s.execute(
                BackfillRun.__table__.select()
                .where(BackfillRun.status == BackfillStatus.RUNNING)
                .limit(1)
            ).first()
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Ein Backfill läuft bereits — bitte warten oder abbrechen.",
                )
    finally:
        engine.dispose()

    client = ComdirectClient()
    try:
        auth_state = await client.begin_auth()
    except RuntimeError as exc:
        logger.error(f"begin_auth failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))

    session_id = str(uuid.uuid4())
    _pending_sessions[session_id] = {
        "kind": "backfill",
        "client": client,
        "session_identifier": auth_state["session_identifier"],
        "challenge_id": auth_state["challenge_id"],
        "config": config,
    }

    logger.info("Backfill TAN challenge sent, session_id=%s", session_id)
    return BackfillStartResponse(status="pending_tan", session_id=session_id)


@app.post("/internal/backfill/confirm", status_code=202)
async def internal_backfill_confirm(
    session_id: str, background_tasks: BackgroundTasks
):
    """Step 2: Complete auth, create BackfillRun row, dispatch to BackgroundTasks."""
    from datetime import date as _date

    from sqlalchemy.orm import Session

    from src.core.db.models import BackfillRun, BackfillStatus

    if session_id not in _pending_sessions:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    pending = _pending_sessions.pop(session_id)
    if pending.get("kind") != "backfill":
        raise HTTPException(
            status_code=400,
            detail=f"Session {session_id} is not a backfill session",
        )

    client: ComdirectClient = pending["client"]
    config = pending.get("config", {})
    months = int(config.get("months", 24))

    ok = await client.complete_auth(
        pending["session_identifier"], pending["challenge_id"]
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Session activation failed")

    today = _date.today()
    target = today.replace(day=1)
    # Roll back `months` whole months (calendar-aware).
    year = target.year
    month = target.month - months
    while month <= 0:
        month += 12
        year -= 1
    target = target.replace(year=year, month=month)

    run_id = str(uuid.uuid4())
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as s:
            s.add(
                BackfillRun(
                    id=run_id,
                    target_start_date=target,
                    status=BackfillStatus.RUNNING,
                )
            )
            s.commit()
    finally:
        engine.dispose()

    background_tasks.add_task(
        _execute_backfill_in_worker, run_id, client, target.isoformat()
    )

    logger.info("Backfill %s dispatched (target=%s, months=%d)", run_id, target, months)
    return {"status": "accepted", "run_id": run_id, "target_start_date": target.isoformat()}


@app.get("/internal/backfill/runs/{run_id}", response_model=BackfillRunResponse)
def internal_backfill_run_status(run_id: str):
    """Return the current state of a backfill run for UI polling."""
    from sqlalchemy.orm import Session

    from src.core.db.models import BackfillRun

    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as s:
            row = s.get(BackfillRun, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Run not found")
            return BackfillRunResponse(
                run_id=row.id,
                status=row.status.value if hasattr(row.status, "value") else str(row.status),
                target_start_date=row.target_start_date.isoformat(),
                current_window_start=(
                    row.current_window_start.isoformat()
                    if row.current_window_start
                    else None
                ),
                windows_total=row.windows_total,
                windows_done=row.windows_done,
                rows_inserted=row.rows_inserted,
                progress_message=row.progress_message,
                error=row.error,
                started_at=row.started_at.isoformat(),
                finished_at=row.finished_at.isoformat() if row.finished_at else None,
            )
    finally:
        engine.dispose()
