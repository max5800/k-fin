"""Bridge from JSON/CSV exports into the raw_transactions staging table.

Produces the canonical row shape expected by
`NormalizationPipeline.load_raw_transactions`, computing the content
hash so Comdirect corrections result in new versioned rows rather than
silent overwrites.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.core.db.models import DataSource, SyncRun, SyncSource, SyncStatus
from src.normalization.canonicalize import canonicalize, content_hash
from src.normalization.pipeline import NormalizationPipeline

logger = logging.getLogger(__name__)


def ingest_json_export(
    pipeline: NormalizationPipeline,
    export_path: Path,
    *,
    batch_id: str | None = None,
    source: DataSource = DataSource.COMDIRECT,
) -> int:
    """Load every transaction from a JSON export into raw_transactions.

    The JSON file is expected to follow the shape produced by
    `src.exporter.json_export.build_export` — a `transactions` dict
    keyed by account id, each mapping to a list of transaction dicts.
    Returns the number of newly-inserted raw rows.
    """
    batch = batch_id or f"json-{export_path.name}"
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    rows = _rows_from_json_payload(payload, batch_id=batch, source=source)
    return _run_ingest(pipeline, rows, batch_id=batch, source=source)


def ingest_transactions(
    pipeline: NormalizationPipeline,
    transactions: Iterable[dict[str, Any]],
    *,
    batch_id: str | None = None,
    source: DataSource = DataSource.COMDIRECT,
) -> int:
    """Ingest an already-parsed list of transaction dicts.

    Useful for tests and for in-process calls where the JSON export is
    held in memory.
    """
    batch = batch_id or f"inproc-{datetime.now(timezone.utc).isoformat()}"
    rows = [_build_row(tx, batch_id=batch, source=source) for tx in transactions]
    return _run_ingest(pipeline, rows, batch_id=batch, source=source)


def _rows_from_json_payload(
    payload: dict, *, batch_id: str, source: DataSource = DataSource.COMDIRECT
) -> list[dict[str, Any]]:
    transactions_by_account = payload.get("transactions") or {}
    rows: list[dict[str, Any]] = []
    for _account_id, txs in transactions_by_account.items():
        for tx in txs:
            rows.append(_build_row(tx, batch_id=batch_id, source=source))
    return rows


def _build_row(
    tx: dict[str, Any], *, batch_id: str, source: DataSource = DataSource.COMDIRECT
) -> dict[str, Any]:
    canonical = canonicalize(tx)
    return {
        "content_hash": content_hash(canonical, source=source.value),
        "source": source,
        "external_id": canonical["external_id"] or None,
        "raw_data": tx,
        "batch_id": batch_id,
    }


def _run_ingest(
    pipeline: NormalizationPipeline,
    rows: list[dict[str, Any]],
    *,
    batch_id: str,
    source: DataSource = DataSource.COMDIRECT,
) -> int:
    run_id = str(uuid.uuid4())
    with Session(pipeline.engine) as session:
        session.add(
            SyncRun(
                id=run_id,
                source=SyncSource.RAW_IMPORT,
                data_source=source,
                status=SyncStatus.RUNNING,
            )
        )
        session.commit()

    try:
        inserted = pipeline.load_raw_transactions(rows)
    except Exception as exc:
        with Session(pipeline.engine) as session:
            session.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(
                    status=SyncStatus.FAILED,
                    finished_at=datetime.now(timezone.utc),
                    error=str(exc),
                )
            )
            session.commit()
        raise

    with Session(pipeline.engine) as session:
        session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                status=SyncStatus.SUCCEEDED,
                finished_at=datetime.now(timezone.utc),
                rows_processed=inserted,
            )
        )
        session.commit()
    logger.info("ingested %d raw rows (batch=%s)", inserted, batch_id)
    return inserted
