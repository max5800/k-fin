"""Dev-only DB tools — wipe transactions and seed a fake dataset.

DANGER: every endpoint here is destructive. Gated by
`settings.dev_tools_enabled` (off by default; force-disabled in production
by `Settings._normalize_settings`). The gate is enforced at router level
via `Depends(require_dev_enabled)` so any endpoint added later inherits
the 404 behaviour automatically. Helm sets the flag to True in
`dev/values.local.yaml`; the public template leaves it false.

Use cases:
  - Reset the dev database between feature tests so behaviour with
    fake-but-realistic data isn't shadowed by real bank history.
  - Trial the refund-aware aggregates / budget-spending UI with edge-case
    transactions (Krankenkassen-Erstattung, Splitwise-Ausgleich, Steuer-
    Rückzahlung, internal transfer, outlier) without waiting on the next
    Comdirect sync.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.core.config import settings
from src.core.db.categories import INCOME_CATCHALL_CATEGORY_ID
from src.core.db.models import (
    Category,
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    SyncRun,
    SyncStage,
    SyncStatus,
)

logger = logging.getLogger(__name__)


def require_dev_enabled() -> None:
    """Router-level guard. Returns 404 (not 401/403) so the dev surface is
    indistinguishable from a non-existent route when the flag is off — the
    response posture is identical for unauthenticated and authenticated
    callers, which prevents fingerprinting whether wipe/seed are armed.
    """
    if not settings.dev_tools_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not Found"
        )


# `/status` is exempt from `require_dev_enabled` so the UI can probe the
# flag without 404'ing; it lives on a sub-router with no guard. The main
# router enforces the guard for every (current and future) endpoint.
status_router = APIRouter(prefix="/dev", tags=["dev"])
router = APIRouter(
    prefix="/dev",
    tags=["dev"],
    dependencies=[Depends(require_dev_enabled)],
)


# Tables flushed by `/dev/wipe`. Order matters only on the diagnostic
# print-out; the SQL itself uses TRUNCATE ... CASCADE so FK direction is
# handled by Postgres.
_WIPE_TABLES = (
    "transaction_tags",
    "reviewed_suggestions",
    "agent_runs",
    "sync_runs",
    "backfill_runs",
    "reports",
    "normalized_transactions",
    "recurring_patterns",
    "raw_transactions",
)


class DevStatus(BaseModel):
    enabled: bool


class WipeResult(BaseModel):
    wiped_tables: list[str]
    transaction_count_before: int
    transaction_count_after: int


class SeedResult(BaseModel):
    inserted_transactions: int
    period_start: date
    period_end: date
    refund_count: int
    internal_transfer_count: int
    outlier_count: int


@status_router.get(
    "/status",
    response_model=DevStatus,
    dependencies=[Depends(require_token)],
)
def dev_status():
    """Authenticated probe — UI reads this to decide whether to show the
    dev panel. Returns enabled=False when the flag is off instead of 404
    so the UI gets a clean negative answer. Auth-gated to avoid leaking
    deployment posture (`app_env`, dev-tools state) to anonymous callers.
    The response intentionally omits `app_env` — the UI only needs the
    boolean, and the env string would tell an attacker whether wipe is
    armed.
    """
    return DevStatus(enabled=settings.dev_tools_enabled)


@router.post("/wipe", response_model=WipeResult, dependencies=[Depends(require_token)])
def wipe_transactions(db: Session = Depends(get_db)):
    """Truncate transaction-related tables.

    Keeps: categories, budgets, users, app_settings, depots, instruments,
    positions, depot_transactions, portfolio_snapshots, alembic_version.
    """
    before = db.query(NormalizedTransaction).count()
    table_list = ", ".join(_WIPE_TABLES)
    # Single CASCADE statement — Postgres handles FK ordering. RESTART
    # IDENTITY resets any sequences (e.g. recurring_patterns.id).
    db.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"))
    db.commit()
    after = db.query(NormalizedTransaction).count()
    logger.warning("dev/wipe executed: %d → %d transactions", before, after)
    return WipeResult(
        wiped_tables=list(_WIPE_TABLES),
        transaction_count_before=before,
        transaction_count_after=after,
    )


# ---------------------------------------------------------------------------
# Seed generator — realistic 6-month fake dataset
# ---------------------------------------------------------------------------


@dataclass
class _TxSpec:
    """A single fake transaction to be persisted."""

    booking_date: date
    amount: Decimal
    sender: str | None
    recipient: str | None
    description: str
    category_id: str | None
    is_refund: bool = False
    is_recurring: bool = False
    is_outlier: bool = False
    internal_transfer: bool = False
    # Optional IBAN overrides — only used by internal-transfer rows so the
    # two legs sit on different accounts (Giro vs Tagesgeld). All other
    # rows default to `_OWN_IBAN` based on the amount sign.
    sender_iban: str | None = None
    recipient_iban: str | None = None


_OWN_NAME = "John Doe"
_OWN_IBAN = "DE00000000000000000000"
_OWN_TAGESGELD_IBAN = "DE00000000000000000001"


def _hash(seed: str, n: int) -> str:
    """Stable 64-char hex string for FK-coupled (raw, normalized) pairs."""
    return hashlib.sha256(f"{seed}-{n}".encode()).hexdigest()


def _add_month(d: date, delta: int) -> date:
    """Add `delta` months, clamping day-of-month to the new month's length
    (so Jan 31 + 1 month → Feb 28/29, not invalid).
    """
    m = d.month - 1 + delta
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _build_seed(today: date) -> list[_TxSpec]:
    """Produce ~150 realistic transactions covering the trailing 6 months.

    Reproducible per call (fixed RNG seed). Specifically tuned to exercise
    the refund-aware aggregates: a Krankenkassen-Erstattung shadows an
    Apotheken-Rechnung in a few months, Splitwise mirrors restaurant tx,
    Steuerrückzahlung lands once as real income, internal transfers must
    not appear in cashflow, and one elektronik purchase is flagged as an
    outlier.
    """
    rng = random.Random(20260508)
    txs: list[_TxSpec] = []

    # Six full months ending in `today.replace(day=1) - 1day` — current
    # month is partial and skipped to keep month-over-month comparisons clean.
    month_start = today.replace(day=1)
    months = [_add_month(month_start, -i) for i in range(6, 0, -1)]

    for m_idx, m in enumerate(months):
        # --- Recurring fixed (income + standing orders) ---
        txs.append(_TxSpec(
            booking_date=m.replace(day=1),
            amount=Decimal("3300.00"),
            sender="Arbeitgeber GmbH",
            recipient=_OWN_NAME,
            description="Gehalt",
            category_id="gehalt",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=1),
            amount=Decimal("-1100.00"),
            sender=_OWN_NAME,
            recipient="Vermieter Hauser",
            description="Miete",
            category_id="miete",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=3),
            amount=Decimal("-45.00"),
            sender=_OWN_NAME,
            recipient="Telekom Deutschland",
            description="Internet & Telefon",
            category_id="internet-telefon",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=5),
            amount=Decimal("-85.00"),
            sender=_OWN_NAME,
            recipient="Stadtwerke",
            description="Strom & Gas",
            category_id="strom-gas",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=7),
            amount=Decimal("-15.99"),
            sender=_OWN_NAME,
            recipient="Netflix International",
            description="Netflix Monatsabo",
            category_id="abos-streaming",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=12),
            amount=Decimal("-9.99"),
            sender=_OWN_NAME,
            recipient="Spotify AB",
            description="Spotify Premium",
            category_id="abos-streaming",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=15),
            amount=Decimal("-75.00"),
            sender=_OWN_NAME,
            recipient="Allianz Versicherung",
            description="Hausrat & Haftpflicht",
            category_id="versicherungen",
            is_recurring=True,
        ))
        txs.append(_TxSpec(
            booking_date=m.replace(day=25),
            amount=Decimal("-300.00"),
            sender=_OWN_NAME,
            recipient="ETF-Sparplan",
            description="ETF-Sparplan MSCI World",
            category_id="etf-sparplan",
            is_recurring=True,
        ))
        if m.month % 3 == 0:
            txs.append(_TxSpec(
                booking_date=m.replace(day=15),
                amount=Decimal("-55.08"),
                sender=_OWN_NAME,
                recipient="Beitragsservice von ARD ZDF",
                description="Rundfunkbeitrag",
                category_id="gez",
                is_recurring=True,
            ))

        # --- Variable: groceries 4-6x ---
        for _ in range(rng.randint(4, 6)):
            day = rng.randint(2, 28)
            amt = -Decimal(str(round(rng.uniform(28.0, 95.0), 2)))
            txs.append(_TxSpec(
                booking_date=m.replace(day=day),
                amount=amt,
                sender=_OWN_NAME,
                recipient=rng.choice(["REWE", "EDEKA", "ALDI SÜD", "LIDL"]),
                description="Einkauf",
                category_id="lebensmittel",
            ))
        # --- Drogerie 1-2x ---
        for _ in range(rng.randint(1, 2)):
            day = rng.randint(2, 28)
            txs.append(_TxSpec(
                booking_date=m.replace(day=day),
                amount=-Decimal(str(round(rng.uniform(15.0, 35.0), 2))),
                sender=_OWN_NAME,
                recipient="DM Drogerie",
                description="Drogerie",
                category_id="drogerie",
            ))

        # --- Tanken 1-2x ---
        for _ in range(rng.randint(1, 2)):
            day = rng.randint(2, 28)
            txs.append(_TxSpec(
                booking_date=m.replace(day=day),
                amount=-Decimal(str(round(rng.uniform(45.0, 80.0), 2))),
                sender=_OWN_NAME,
                recipient=rng.choice(["ARAL", "Shell", "Total"]),
                description="Tanken",
                category_id="tanken",
            ))

        # --- Restaurant 3-5x, sometimes with Splitwise refund ---
        for _ in range(rng.randint(3, 5)):
            day = rng.randint(2, 28)
            amt = -Decimal(str(round(rng.uniform(18.0, 75.0), 2)))
            txs.append(_TxSpec(
                booking_date=m.replace(day=day),
                amount=amt,
                sender=_OWN_NAME,
                recipient=rng.choice([
                    "Pizzeria Da Mario",
                    "Sushi Yamato",
                    "Café Einstein",
                    "Burger Joint",
                    "Brauhaus am Markt",
                ]),
                description="Restaurant",
                category_id="restaurant-cafe",
            ))
        # Splitwise refund 1x in ~half the months — Original-Kategorie
        # restaurant-cafe + is_refund=True so the budget nets correctly.
        if rng.random() < 0.5:
            day = rng.randint(20, 28)
            txs.append(_TxSpec(
                booking_date=m.replace(day=day),
                amount=Decimal(str(round(rng.uniform(15.0, 40.0), 2))),
                sender="Splitwise (Anna Müller)",
                recipient=_OWN_NAME,
                description="Splitwise Ausgleich Restaurant",
                category_id="restaurant-cafe",
                is_refund=True,
            ))

        # --- Apotheke + Krankenkasse refund pair ---
        if rng.random() < 0.7:
            apo_day = rng.randint(5, 18)
            apo_amt = -Decimal(str(round(rng.uniform(25.0, 65.0), 2)))
            txs.append(_TxSpec(
                booking_date=m.replace(day=apo_day),
                amount=apo_amt,
                sender=_OWN_NAME,
                recipient="Apotheke am Markt",
                description="Apotheke Rezept",
                category_id="gesundheit",
            ))
            # Refund follows in ~half the months, partial amount.
            if rng.random() < 0.5:
                ref_day = min(28, apo_day + rng.randint(7, 14))
                ref_amt = -apo_amt * Decimal(str(round(rng.uniform(0.4, 0.7), 2)))
                txs.append(_TxSpec(
                    booking_date=m.replace(day=ref_day),
                    amount=ref_amt.quantize(Decimal("0.01")),
                    sender="Techniker Krankenkasse",
                    recipient=_OWN_NAME,
                    description="Erstattung Rezept",
                    category_id="gesundheit",
                    is_refund=True,
                ))

        # --- Cashback monthly: real income, NOT a refund ---
        txs.append(_TxSpec(
            booking_date=m.replace(day=28),
            amount=Decimal(str(round(rng.uniform(2.5, 8.0), 2))),
            sender="Visa Cashback Programm",
            recipient=_OWN_NAME,
            description="Cashback",
            category_id=INCOME_CATCHALL_CATEGORY_ID,
        ))

    # --- Special events: spread across the 6-month window -------------------

    # Steuerrückzahlung — real income, not a refund.
    txs.append(_TxSpec(
        booking_date=months[1].replace(day=14),
        amount=Decimal("847.50"),
        sender="Finanzamt München",
        recipient=_OWN_NAME,
        description="Einkommensteuer 2025",
        category_id=INCOME_CATCHALL_CATEGORY_ID,
    ))

    # Amazon retoure — refund on original kleidung purchase.
    purchase_month = months[2]
    refund_month = months[3]
    txs.append(_TxSpec(
        booking_date=purchase_month.replace(day=10),
        amount=Decimal("-89.99"),
        sender=_OWN_NAME,
        recipient="Amazon EU",
        description="Bestellung Schuhe",
        category_id="kleidung",
    ))
    txs.append(_TxSpec(
        booking_date=refund_month.replace(day=2),
        amount=Decimal("89.99"),
        sender="Amazon EU",
        recipient=_OWN_NAME,
        description="Retoure Schuhe",
        category_id="kleidung",
        is_refund=True,
    ))

    # Reisen — single bigger trip three months back.
    txs.append(_TxSpec(
        booking_date=months[2].replace(day=20),
        amount=Decimal("-650.00"),
        sender=_OWN_NAME,
        recipient="Booking.com",
        description="Wochenende Wien",
        category_id="reisen",
    ))

    # Outlier: big elektronik purchase.
    txs.append(_TxSpec(
        booking_date=months[4].replace(day=8),
        amount=Decimal("-1349.00"),
        sender=_OWN_NAME,
        recipient="MediaMarkt",
        description="Laptop Lenovo ThinkPad",
        category_id="elektronik",
        is_outlier=True,
    ))

    # Internal transfer pair: -500 from Giro, +500 to Tagesgeld. The two
    # legs need distinct IBANs so a future internal-transfer-by-IBAN
    # detector sees a real Giro→Tagesgeld move rather than a self-loop.
    transfer_month = months[3]
    transfer_day = transfer_month.replace(day=18)
    txs.append(_TxSpec(
        booking_date=transfer_day,
        amount=Decimal("-500.00"),
        sender=_OWN_NAME,
        recipient=f"{_OWN_NAME} Tagesgeld",
        description="Umbuchung Tagesgeld",
        category_id="umbuchung",
        internal_transfer=True,
        sender_iban=_OWN_IBAN,
        recipient_iban=_OWN_TAGESGELD_IBAN,
    ))
    txs.append(_TxSpec(
        booking_date=transfer_day,
        amount=Decimal("500.00"),
        sender=f"{_OWN_NAME} Tagesgeld",
        recipient=_OWN_NAME,
        description="Umbuchung Tagesgeld",
        category_id="umbuchung",
        internal_transfer=True,
        sender_iban=_OWN_TAGESGELD_IBAN,
        recipient_iban=_OWN_IBAN,
    ))

    # Sort by date so the DB layout matches a real sync.
    txs.sort(key=lambda t: t.booking_date)
    return txs


@router.post("/seed", response_model=SeedResult, dependencies=[Depends(require_token)])
def seed_dataset(db: Session = Depends(get_db)):
    """Insert ~150 realistic fake transactions across the trailing 6 months.

    Idempotent in spirit: each call generates a fresh batch with a new
    `seed_run_id`, so content_hashes never collide with previous seeds. To
    start clean, call `/dev/wipe` first.
    """
    # Sanity: the categories must exist (they're seeded by migration 0005).
    # If somebody nuked them, fail loudly rather than producing orphan tx.
    expected = {
        "gehalt", "miete", "internet-telefon", "strom-gas", "abos-streaming",
        "versicherungen", "etf-sparplan", "gez", "lebensmittel", "drogerie",
        "tanken", "restaurant-cafe", "gesundheit", INCOME_CATCHALL_CATEGORY_ID,
        "kleidung", "reisen", "elektronik", "umbuchung",
    }
    have = {c.id for c in db.query(Category).all()}
    missing = expected - have
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Categories missing — run migrations first: {sorted(missing)}",
        )

    seed_run_id = uuid.uuid4().hex[:12]
    today = date.today()
    specs = _build_seed(today)

    # One SyncRun row to anchor this seed batch — useful for the runs UI to
    # show "fake-data injection" as a discrete event rather than inserts
    # appearing out of nowhere.
    sync_run_id = f"seed-{seed_run_id}"
    db.add(SyncRun(
        id=sync_run_id,
        source=SyncStage.RAW_IMPORT,
        data_source=DataSource.COMDIRECT,
        status=SyncStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        rows_processed=len(specs),
    ))

    # Insert raw rows first and flush — SQLAlchemy's per-model insertmany
    # would otherwise emit normalized rows before the raw rows they
    # reference, tripping the FK constraint.
    for i, spec in enumerate(specs):
        content_hash = _hash(seed_run_id, i)
        db.add(RawTransaction(
            content_hash=content_hash,
            source=DataSource.COMDIRECT,
            external_id=f"SEED-{seed_run_id}-{i:04d}",
            raw_data={"seed": True, "seed_run_id": seed_run_id, "index": i},
        ))
    db.flush()

    for i, spec in enumerate(specs):
        content_hash = _hash(seed_run_id, i)
        db.add(NormalizedTransaction(
            id=content_hash,
            raw_content_hash=content_hash,
            source=DataSource.COMDIRECT,
            external_id=f"SEED-{seed_run_id}-{i:04d}",
            booking_date=spec.booking_date,
            valuation_date=spec.booking_date,
            amount=spec.amount,
            currency="EUR",
            sender=spec.sender,
            recipient=spec.recipient,
            sender_iban=spec.sender_iban
            if spec.sender_iban is not None
            else (_OWN_IBAN if spec.amount < 0 else None),
            recipient_iban=spec.recipient_iban
            if spec.recipient_iban is not None
            else (_OWN_IBAN if spec.amount > 0 else None),
            description=spec.description,
            category_id=spec.category_id,
            is_recurring=spec.is_recurring,
            is_outlier=spec.is_outlier,
            internal_transfer=spec.internal_transfer,
            is_refund=spec.is_refund,
        ))
    db.commit()

    refund_count = sum(1 for s in specs if s.is_refund)
    transfer_count = sum(1 for s in specs if s.internal_transfer)
    outlier_count = sum(1 for s in specs if s.is_outlier)
    logger.warning(
        "dev/seed inserted %d transactions (refunds=%d, transfers=%d, outliers=%d)",
        len(specs), refund_count, transfer_count, outlier_count,
    )

    return SeedResult(
        inserted_transactions=len(specs),
        period_start=specs[0].booking_date,
        period_end=specs[-1].booking_date,
        refund_count=refund_count,
        internal_transfer_count=transfer_count,
        outlier_count=outlier_count,
    )
