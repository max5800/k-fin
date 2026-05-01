"""Historical-backfill walker for Comdirect account transactions.

Walks date-windows backwards from today to ``target_start_date`` per
account, ingesting each window via the existing raw-transaction pipeline.
Idempotent thanks to ``content_hash`` dedup in the ingest layer — re-runs
are gefahrlos.

Comdirect API constraints (probed 2026-05-01):
- ``paging-first`` (offset paging) returns HTTP 422 — cannot be used.
- ``min-bookingDate`` + ``max-bookingDate`` work and bound the response.
- ``min-bookingDate`` more than ~3y back returns HTTP 422 — treated as
  a soft stop (boundary reached).

The walker writes progress to a ``BackfillRun`` row continuously so the
UI can poll status without long-polling the worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.connector.comdirect_client import ComdirectClient
from src.connector.models import ComdirectTransaction
from src.core.db.models import BackfillRun, BackfillStatus
from src.normalization.ingest import ingest_transactions
from src.normalization.pipeline import NormalizationPipeline

logger = logging.getLogger(__name__)


# Maximum number of "halve-the-window" retries before giving up on a window.
# 30 days → 15 → 7 → 3 → 1 → 0 (abort): 5 halvings is plenty for 500-cap relief.
MAX_HALVINGS = 5

# Width of the initial probe window per account, in days.
INITIAL_WINDOW_DAYS = 30


@dataclass
class WindowResult:
    rows_inserted: int
    matches: int
    truncated: bool  # True if API returned >= paging_count rows


@dataclass
class BackfillSummary:
    windows_done: int
    rows_inserted: int
    accounts_processed: int


def count_planned_windows(
    accounts: list[str],
    target_start_date: date,
    *,
    today: date | None = None,
    initial_window_days: int = INITIAL_WINDOW_DAYS,
) -> int:
    """Estimate total windows for the progress bar.

    Halvings inflate the real count at runtime — that's fine, the UI just
    shows ``done / total`` and the percentage may briefly go past 100%.
    """
    today = today or date.today()
    total_days = max(1, (today - target_start_date).days)
    per_account = (total_days + initial_window_days - 1) // initial_window_days
    return per_account * len(accounts)


async def _ingest_window_values(
    pipeline: NormalizationPipeline,
    account_id: str,
    values: list[dict],
    *,
    run_id: str,
    window_start: date,
    window_end: date,
) -> int:
    """Run the existing ingest pipeline on one window's worth of values.

    Comdirect's raw account-transaction shape has ``amount: {value, unit}``
    while ``canonicalize`` expects either a flat ``amount: float`` or a
    Depot-style ``transactionValue`` dict. The regular sync flow flattens
    via the Pydantic model in ``json_export.build_export``; we mirror
    that here so canonicalize sees a shape it can parse.
    """
    if not values:
        return 0
    flat_values = [
        ComdirectTransaction.model_validate(v).model_dump() for v in values
    ]
    batch_id = f"backfill-{run_id}-{account_id}-{window_start.isoformat()}"
    return ingest_transactions(pipeline, flat_values, batch_id=batch_id)


async def _walk_account(
    *,
    client: ComdirectClient,
    pipeline: NormalizationPipeline,
    db_session: Session,
    run_id: str,
    account_id: str,
    target_start_date: date,
    today: date,
    initial_window_days: int,
    paging_count: int,
    on_progress: Optional[Callable[[date, int, int], Awaitable[None]]] = None,
) -> tuple[int, int]:
    """Walk one account backwards, ingesting each window. Returns (rows, windows_done)."""
    rows_inserted_total = 0
    windows_done = 0

    window_end = today
    window_days = initial_window_days

    while window_end >= target_start_date:
        window_start = max(target_start_date, window_end - timedelta(days=window_days - 1))

        values, matches = await client.get_transactions_window(
            account_id, window_start, window_end, paging_count=paging_count
        )

        truncated = len(values) >= paging_count

        if truncated:
            # Halve and retry the same window_end with smaller width.
            new_width = max(1, window_days // 2)
            halvings = 0
            while truncated and halvings < MAX_HALVINGS and new_width >= 1:
                logger.warning(
                    "backfill[%s] window %s..%s truncated at %d rows (matches=%s) — "
                    "halving width %d → %d",
                    account_id,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    len(values),
                    matches,
                    window_days,
                    new_width,
                )
                window_days = new_width
                window_start = max(
                    target_start_date, window_end - timedelta(days=window_days - 1)
                )
                values, matches = await client.get_transactions_window(
                    account_id, window_start, window_end, paging_count=paging_count
                )
                truncated = len(values) >= paging_count
                new_width = max(1, window_days // 2)
                halvings += 1

            if truncated:
                # Even single-day window can't fit — extremely rare, abort run.
                raise RuntimeError(
                    f"backfill[{account_id}]: window {window_start}..{window_end} "
                    f"still truncated after {MAX_HALVINGS} halvings — "
                    f">{paging_count} transactions on a single day?"
                )

        rows = await _ingest_window_values(
            pipeline,
            account_id,
            values,
            run_id=run_id,
            window_start=window_start,
            window_end=window_end,
        )
        rows_inserted_total += rows
        windows_done += 1

        # Persist progress immediately so the UI sees movement.
        db_session.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id)
            .values(
                current_window_start=window_start,
                windows_done=BackfillRun.windows_done + 1,
                rows_inserted=BackfillRun.rows_inserted + rows,
                progress_message=(
                    f"{account_id[:6]}…: {window_start}..{window_end} (+{rows})"
                ),
            )
        )
        db_session.commit()

        if on_progress:
            await on_progress(window_start, rows, matches)

        # Step back one day before the current window_start.
        if window_start <= target_start_date:
            break
        window_end = window_start - timedelta(days=1)
        # Reset to default width after a successful (non-halved) window.
        window_days = initial_window_days

    return rows_inserted_total, windows_done


async def run_backfill(
    *,
    client: ComdirectClient,
    pipeline: NormalizationPipeline,
    run_id: str,
    target_start_date: date,
    account_ids: list[str],
    today: date | None = None,
    initial_window_days: int = INITIAL_WINDOW_DAYS,
    paging_count: int = 500,
) -> BackfillSummary:
    """Drive the full backfill across multiple accounts.

    Updates the BackfillRun row continuously. Runs final
    ``process_and_normalize`` once at the end (more efficient than
    per-window). Caller is responsible for terminal status writes —
    this function raises on failure rather than swallowing.
    """
    today = today or date.today()
    if target_start_date >= today:
        raise ValueError(
            f"target_start_date ({target_start_date}) must be before today ({today})"
        )

    rows_total = 0
    windows_total = 0
    accounts_processed = 0

    with Session(pipeline.engine) as db_session:
        # Set up totals up front so the UI knows the scale.
        planned = count_planned_windows(
            account_ids, target_start_date, today=today,
            initial_window_days=initial_window_days,
        )
        db_session.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id)
            .values(windows_total=planned)
        )
        db_session.commit()

        for account_id in account_ids:
            logger.info("backfill[%s]: walking %s..%s", account_id, target_start_date, today)
            rows, windows = await _walk_account(
                client=client,
                pipeline=pipeline,
                db_session=db_session,
                run_id=run_id,
                account_id=account_id,
                target_start_date=target_start_date,
                today=today,
                initial_window_days=initial_window_days,
                paging_count=paging_count,
            )
            rows_total += rows
            windows_total += windows
            accounts_processed += 1

    # One normalize pass at the end — covers everything we just ingested.
    if rows_total > 0:
        logger.info("backfill[%s]: normalizing after %d new raw rows", run_id, rows_total)
        pipeline.process_and_normalize()

    return BackfillSummary(
        windows_done=windows_total,
        rows_inserted=rows_total,
        accounts_processed=accounts_processed,
    )


def mark_run_terminal(
    pipeline: NormalizationPipeline,
    run_id: str,
    *,
    status: BackfillStatus,
    error: str | None = None,
) -> None:
    """Write terminal status + finished_at on the BackfillRun row."""
    with Session(pipeline.engine) as db_session:
        db_session.execute(
            update(BackfillRun)
            .where(BackfillRun.id == run_id)
            .values(
                status=status,
                error=error,
                finished_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()


def has_running_backfill(pipeline: NormalizationPipeline) -> bool:
    """True if any BackfillRun row currently has status=RUNNING."""
    with Session(pipeline.engine) as db_session:
        result = db_session.execute(
            BackfillRun.__table__.select().where(
                BackfillRun.status == BackfillStatus.RUNNING
            ).limit(1)
        ).first()
        return result is not None
