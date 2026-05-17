"""CSV import — bring an exported account statement into k-fin.

Some sources have no API k-fin can reach: a PayPal *personal* account
cannot use the Transaction Search API (that needs a business account),
so the user exports PayPal's own "Kontoauszug" CSV and uploads it here —
see :mod:`src.normalization.paypal_csv`.

Unlike the bank-sync routes, a CSV import needs **no worker hop**: a
user-uploaded file carries no bank secrets, so the api service parses,
ingests and normalizes it directly. The endpoint is synchronous —
FastAPI runs it in a worker thread, so the CPU/DB-bound normalization
pass does not block the event loop.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from src.api.deps import Auth
from src.core.config import settings
from src.core.db.models import DataSource
from src.external.provider import CanonicalTransaction
from src.normalization.ingest import ingest_canonical
from src.normalization.paypal_csv import (
    PayPalCsvError,
    parse_paypal_csv,
    paypal_csv_canonicalize,
)
from src.normalization.pipeline import NormalizationPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/import", tags=["import"], dependencies=[Auth])

# A twelve-month PayPal Kontoauszug is a few hundred KB; cap well above
# that, low enough to reject a mis-uploaded file outright.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class PayPalImportResult(BaseModel):
    """Summary of a PayPal CSV import, for the UI."""

    parsed: int       # real transactions read (funding/plumbing rows skipped)
    inserted: int     # newly-inserted raw rows
    duplicates: int   # rows already present from an earlier upload
    normalized: int   # rows in normalized_transactions after the run


@router.post("/paypal-csv", response_model=PayPalImportResult)
def import_paypal_csv(file: UploadFile = File(...)) -> PayPalImportResult:
    """Import a PayPal "Kontoauszug" CSV export.

    Parses the upload (funding/plumbing rows are skipped by the parser),
    ingests the real transactions, then re-runs normalization so the
    cross-source merchant enrichment picks them up. Re-uploading an
    overlapping export is idempotent — ``(source, external_id)`` dedup
    absorbs the overlap. A malformed CSV is a 422 with an actionable
    message (which column is missing, which row failed).
    """
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )

    try:
        rows = parse_paypal_csv(raw)
    except PayPalCsvError as exc:
        # A schema / parse problem is the user's CSV, not a server fault.
        raise HTTPException(status_code=422, detail=str(exc))

    transactions = [
        CanonicalTransaction(
            canonical=paypal_csv_canonicalize(row),
            raw_data=row,
            source=DataSource.PAYPAL,
        )
        for row in rows
    ]

    pipeline = NormalizationPipeline(
        database_url=settings.database_url,
        own_ibans=settings.get_own_ibans(),
    )
    try:
        inserted = ingest_canonical(pipeline, transactions, source=DataSource.PAYPAL)
        df, _run_id = pipeline.process_and_normalize()
    finally:
        pipeline.engine.dispose()

    logger.info(
        "PayPal CSV import: %d parsed, %d inserted, %d normalized",
        len(transactions),
        inserted,
        len(df),
    )
    return PayPalImportResult(
        parsed=len(transactions),
        inserted=inserted,
        duplicates=len(transactions) - inserted,
        normalized=len(df),
    )
