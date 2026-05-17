"""Statement import — bring an exported account statement into k-fin.

Some sources have no API k-fin can reach. A PayPal *personal* account
cannot use the Transaction Search API (that needs a business account), so
the user uploads PayPal's own "Kontoauszug" CSV export (see
:mod:`src.normalization.paypal_csv`). Santander's PSD2 / XS2A interface
does not expose the 1plus Visa credit card, so the card is uploaded as the
monthly MySantander statement PDFs (see :mod:`src.normalization.santander_pdf`).

Unlike the bank-sync routes, a statement import needs **no worker hop**: a
user-uploaded file carries no bank secrets, so the api service parses,
ingests and normalizes it directly. The endpoints are synchronous —
FastAPI runs them in a worker thread, so the CPU/DB-bound normalization
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
from src.normalization.santander_canonicalize import santander_canonicalize
from src.normalization.santander_pdf import SantanderPdfError, parse_statement

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

    pipeline = NormalizationPipeline(database_url=settings.database_url)
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


class SantanderImportResult(BaseModel):
    """Summary of a Santander statement-PDF import, for the UI."""

    statements: int    # PDF files parsed successfully
    parsed: int        # transactions read across all statements
    inserted: int      # newly-inserted raw rows
    duplicates: int    # rows already present from an earlier upload
    normalized: int    # rows in normalized_transactions after the run
    errors: list[str]  # per-file failures — a bad file is skipped, not fatal


@router.post("/santander-pdf", response_model=SantanderImportResult)
def import_santander_pdf(
    files: list[UploadFile] = File(...),
) -> SantanderImportResult:
    """Import one or more Santander 1plus-Card statement PDFs.

    Santander's PSD2 / XS2A interface does not expose the 1plus Visa credit
    card, so the card is ingested from the monthly MySantander statement
    PDFs. Several statements upload in one call; each is parsed
    independently — a file that is not a recognisable statement, or one
    that fails its opening/closing balance check, is reported in ``errors``
    and skipped while the rest still import. Re-uploading a statement is
    idempotent: ``(source, external_id)`` dedup absorbs the overlap.

    A 422 is returned only when *no* statement could be parsed at all
    (every upload empty, oversized or unreadable); otherwise a partial
    result is returned with the per-file ``errors``.
    """
    transactions: list[CanonicalTransaction] = []
    errors: list[str] = []
    statements = 0

    for upload in files:
        name = upload.filename or "statement.pdf"
        raw = upload.file.read()
        if not raw:
            errors.append(f"{name}: the file is empty")
            continue
        if len(raw) > _MAX_UPLOAD_BYTES:
            errors.append(
                f"{name}: exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
            )
            continue
        try:
            rows = parse_statement(raw)
        except SantanderPdfError as exc:
            # A bad / unreadable file is the user's PDF, not a server fault —
            # report it and keep importing the remaining statements.
            errors.append(f"{name}: {exc}")
            continue
        statements += 1
        transactions.extend(
            CanonicalTransaction(
                canonical=santander_canonicalize(row),
                raw_data=row,
                source=DataSource.SANTANDER_CC,
            )
            for row in rows
        )

    if statements == 0:
        raise HTTPException(
            status_code=422,
            detail="; ".join(errors) or "no statement PDF was uploaded",
        )

    pipeline = NormalizationPipeline(database_url=settings.database_url)
    try:
        inserted = ingest_canonical(
            pipeline, transactions, source=DataSource.SANTANDER_CC
        )
        df, _run_id = pipeline.process_and_normalize()
    finally:
        pipeline.engine.dispose()

    logger.info(
        "Santander PDF import: %d statement(s), %d parsed, %d inserted, %d normalized",
        statements,
        len(transactions),
        inserted,
        len(df),
    )
    return SantanderImportResult(
        statements=statements,
        parsed=len(transactions),
        inserted=inserted,
        duplicates=len(transactions) - inserted,
        normalized=len(df),
        errors=errors,
    )
