"""comdirect-worker — internal sync service.

Runs on port 8001, never exposed to the internet.
Receives sync requests from the public comdirect-api service.
"""

import asyncio
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
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
from src.external.comdirect_client import ComdirectClient
from src.external.provider import BankProvider
from src.external.providers import get_provider
from src.external.yfinance_client import HistoryProvider, YFinanceClient
from src.core.config import settings
from src.core.db.models import DataSource
from src.core.logging import get_logger, setup_logging
from src.core.notifier import notify_failure_from_db
from src.services.portfolio_prices import (
    CurrencyMismatchProviderError,
    InstrumentNotFoundError,
    InvalidPriceRangeError,
    PriceProviderError,
    TickerNotConfiguredError,
    backfill_instrument_prices,
)

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

# A pending TAN session pins a live provider / bank client (an open
# httpx.AsyncClient) in memory. If the user never resolves the TAN, the
# entry would leak for the worker's whole lifetime. Each session records
# a `created_at`; `_evict_stale_sessions` drops the abandoned ones on the
# next session-creating call, releasing the client for garbage collection.
_SESSION_TTL_SECONDS = 900  # 15 minutes


def _evict_stale_sessions() -> None:
    """Drop pending TAN sessions older than `_SESSION_TTL_SECONDS`.

    Called before a new session is opened. A session the user abandoned
    (never completed the TAN) would otherwise pin its provider / bank
    client forever; dropping the dict entry releases it so the GC can
    reclaim the underlying socket.
    """
    now = time.monotonic()
    stale = [
        sid
        for sid, entry in _pending_sessions.items()
        if now - entry.get("created_at", now) > _SESSION_TTL_SECONDS
    ]
    for sid in stale:
        _pending_sessions.pop(sid, None)
        logger.warning(
            "Evicted stale pending session %s — TAN not completed within %ds",
            sid,
            _SESSION_TTL_SECONDS,
        )


# ---------------------------------------------------------------------------
# Shared SQLAlchemy engine — built once in lifespan, reused per request
# ---------------------------------------------------------------------------


def get_engine(request: Request) -> Engine:
    """FastAPI dependency: return the shared engine attached to app.state.

    Per-request ``create_engine`` was costing 50–200ms per call and
    discarding the connection pool every time. The lifespan already
    builds one engine and stows it on ``app.state.engine``; handlers
    now consume that via this dep so the pool actually serves its
    purpose.

    Tests that exercise these handlers via TestClient must seed
    ``app.state.engine`` themselves (the lifespan only runs when
    ``with TestClient(app):`` is used as a context manager). The
    helper :func:`set_test_engine` exists for that.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        # Defensive: should never happen in production. Surface as 503
        # rather than 500 so the api caller can retry once the worker
        # has finished booting.
        raise HTTPException(
            status_code=503,
            detail="Worker engine not initialised — startup may still be running.",
        )
    return engine


def set_test_engine(engine: Engine) -> None:
    """Test helper: bind an engine to ``app.state`` without running lifespan.

    The TestClient swallows lifespan unless used as a context manager,
    which most existing worker tests don't bother with. Calling this
    from a fixture preserves the legacy "monkeypatch create_engine"
    workflow without poking at app internals from each test file.
    """
    app.state.engine = engine

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
    engine: Engine = Depends(get_engine),
):
    """Fetch missing daily close prices for an instrument from yfinance.

    Internal-only — the api dispatcher (`POST /portfolio/instruments/{isin}/
    backfill-prices`) has already validated auth and basic input shape.
    Delegates the fetch + DB upsert to
    :func:`src.services.portfolio_prices.backfill_instrument_prices` so
    the public api never makes outbound to Yahoo Finance and the api
    NetworkPolicy can stay tight.
    """
    with Session(engine) as db:
        try:
            result = backfill_instrument_prices(
                db,
                isin=payload.isin,
                from_date=payload.from_date,
                to_date=payload.to_date,
                provider=provider,
            )
        except (InvalidPriceRangeError, TickerNotConfiguredError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InstrumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Instrument not found") from exc
        except CurrencyMismatchProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PriceProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _InternalBackfillResult(
        isin=result.isin,
        ticker_symbol=result.ticker_symbol,
        requested_from=result.requested_from,
        requested_to=result.requested_to,
        fetched_points=result.fetched_points,
        inserted_points=result.inserted_points,
        skipped_existing=result.skipped_existing,
        source=result.source,
    )


@app.post("/internal/normalize")
def internal_normalize():
    """Re-run normalization pipeline over existing raw_transactions.

    The pipeline owns its own SQLAlchemy engine internally — we can't
    reuse ``app.state.engine`` here without changing the pipeline
    contract. Dispose explicitly so the per-request pool is freed
    instead of leaking until GC.
    """
    from src.normalization.pipeline import NormalizationPipeline

    pipeline = NormalizationPipeline(database_url=settings.database_url)
    try:
        df, run_id = pipeline.process_and_normalize()
        return {"status": "done", "normalized": len(df), "run_id": run_id}
    finally:
        pipeline.engine.dispose()


def _parse_source(source_id: str) -> DataSource:
    """Map a ``source_id`` path segment onto a :class:`DataSource`, or 404."""
    try:
        return DataSource(source_id)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"Unknown sync source: {source_id!r}"
        )


async def _finalize_provider_sync(
    provider: BankProvider,
    source: DataSource,
    *,
    engine: Engine,
    session_id: str,
) -> dict:
    """Stream a provider's canonical transactions, ingest + normalize them,
    then run the provider's post-sync hook.

    Shared by both the TAN-completion route and (for providers that need
    no user step) the start route's inline path.

    A failed *fetch* (auth / provider API) is a hard 502. A failure to
    *persist* an otherwise-good fetch is best-effort — the response is
    still 200 with ``ingest: null``, matching the legacy sync contract.
    """
    from src.normalization.ingest import ingest_and_normalize

    # --- Fetch: auth + provider API. Hard failure. ----------------------
    try:
        transactions = [ct async for ct in provider.complete_sync(None)]
    except Exception as exc:
        logger.exception("Sync fetch failed for source=%s", source.value)
        notify_failure_from_db(
            engine,
            run_kind="sync",
            run_id=session_id,
            error_message=f"Sync failed: {exc}",
        )
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}")

    # --- Persist: ingest + normalize. Best-effort. ----------------------
    ingest_result: dict | None = None
    try:
        inserted, _df, _normalize_run_id = ingest_and_normalize(
            transactions, source=source, database_url=settings.database_url
        )
        logger.info("Ingested %d raw transactions from %s", inserted, source.value)
        logger.info("Normalization completed (%d rows)", len(_df))

        ingest_result = {
            "inserted": inserted,
            "normalized": len(_df),
            "normalize_run_id": _normalize_run_id,
        }
    except Exception:
        logger.exception(
            "Ingest/normalization failed for %s (fetch still succeeded)", source.value
        )

    # --- Provider-specific tail (Comdirect: CSV export + depot ingest).
    #     The hook guards itself; this try is belt-and-braces. The agent
    #     pipeline is intentionally NOT triggered here — sync only fetches
    #     + normalizes; LLM agents run separately via POST /api/v1/runs/full.
    try:
        extra = await provider.post_sync_hook(engine)
        if ingest_result is not None and extra:
            ingest_result.update(extra)
    except Exception:
        logger.exception("Provider post-sync hook failed for %s", source.value)

    logger.info("Sync completed for source=%s", source.value)
    return {
        "status": "done",
        "message": "Sync abgeschlossen — Agents separat starten",
        "ingest": ingest_result,
    }


@app.post("/internal/sync/{source_id}")
async def internal_sync_start(
    source_id: str,
    payload: SyncStartRequest | None = None,
    engine: Engine = Depends(get_engine),
):
    """Step 1: open a provider sync session and trigger its TAN challenge.

    Routes ``source_id`` through the provider registry. A provider whose
    ``tan_kind`` needs no user step runs the whole sync inline and
    returns ``status=done``; Comdirect issues a decoupled pushTAN
    challenge and returns ``status=pending_tan`` with a ``provider`` block
    the UI modal renders.
    """
    _evict_stale_sessions()
    source = _parse_source(source_id)
    provider_cls = get_provider(source)
    config = payload.model_dump(exclude_none=True) if payload else {}
    provider = provider_cls(config=config)

    try:
        challenge = await provider.start_sync()
    except RuntimeError as exc:
        logger.error("start_sync failed for %s: %s", source.value, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    if challenge is None:
        # No user interaction needed (e.g. PayPal m2m-OAuth) — sync inline.
        logger.info("Source %s needs no TAN — running sync inline", source.value)
        return await _finalize_provider_sync(
            provider, source, engine=engine, session_id=str(uuid.uuid4())
        )

    session_id = str(uuid.uuid4())
    _pending_sessions[session_id] = {
        "kind": "sync",
        "source": source,
        "provider": provider,
        "created_at": time.monotonic(),
    }
    logger.info("TAN challenge sent for %s, session_id=%s", source.value, session_id)
    return {
        "status": "pending_tan",
        "session_id": session_id,
        "provider": {
            "source": source.value,
            "display_name": provider_cls.display_name,
            "tan_kind": provider_cls.tan_kind.value,
            "display_hint": challenge.display_hint,
        },
    }


@app.post("/internal/sync/{source_id}/complete")
async def internal_sync_complete(
    source_id: str,
    session_id: str,
    engine: Engine = Depends(get_engine),
):
    """Step 2: complete the provider sync after the user resolved the TAN."""
    source = _parse_source(source_id)
    if session_id not in _pending_sessions:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    pending = _pending_sessions.pop(session_id)
    if pending.get("kind") != "sync":
        raise HTTPException(
            status_code=400, detail=f"Session {session_id} is not a sync session"
        )
    if pending["source"] != source:
        raise HTTPException(
            status_code=400, detail="source_id does not match the session"
        )

    return await _finalize_provider_sync(
        pending["provider"], source, engine=engine, session_id=session_id
    )


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

    pipeline = NormalizationPipeline(database_url=settings.database_url)

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
async def internal_backfill_start(
    payload: BackfillStartRequest | None = None,
    engine: Engine = Depends(get_engine),
):
    """Step 1: Begin backfill auth flow and trigger TAN challenge.

    Refuses (409) if any backfill is already running — max one per user.
    """
    from sqlalchemy.orm import Session

    from src.core.db.models import BackfillRun, BackfillStatus

    config = payload.model_dump() if payload else {"months": 24}

    # Concurrency guard: refuse if a run is already in-flight.
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

    _evict_stale_sessions()
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
        "created_at": time.monotonic(),
    }

    logger.info("Backfill TAN challenge sent, session_id=%s", session_id)
    return BackfillStartResponse(status="pending_tan", session_id=session_id)


@app.post("/internal/backfill/confirm", status_code=202)
async def internal_backfill_confirm(
    session_id: str,
    background_tasks: BackgroundTasks,
    engine: Engine = Depends(get_engine),
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
    with Session(engine) as s:
        s.add(
            BackfillRun(
                id=run_id,
                target_start_date=target,
                status=BackfillStatus.RUNNING,
            )
        )
        s.commit()

    background_tasks.add_task(
        _execute_backfill_in_worker, run_id, client, target.isoformat()
    )

    logger.info("Backfill %s dispatched (target=%s, months=%d)", run_id, target, months)
    return {"status": "accepted", "run_id": run_id, "target_start_date": target.isoformat()}


@app.get("/internal/backfill/runs/{run_id}", response_model=BackfillRunResponse)
def internal_backfill_run_status(
    run_id: str,
    engine: Engine = Depends(get_engine),
):
    """Return the current state of a backfill run for UI polling."""
    from sqlalchemy.orm import Session

    from src.core.db.models import BackfillRun

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
