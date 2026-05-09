"""SQLAlchemy models for the M5 normalization layer.

Design notes:
- RawTransaction uses a content-hash as primary key. The same Comdirect
  transaction ID may produce multiple raw rows over time if the upstream
  data gets corrected; older revisions are linked via `superseded_by`.
- Monetary amounts are stored as Decimal, never float.
- Schema evolution happens through Alembic migrations; no `create_all()`
  is called anywhere in application code.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class TypeEnum(str, enum.Enum):
    FIX = "fix"
    VARIABEL = "variabel"
    DISKRETIONAER = "diskretionaer"


class SyncStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncSource(str, enum.Enum):
    RAW_IMPORT = "raw_import"
    NORMALIZE = "normalize"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(str, enum.Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    WEBHOOK = "webhook"


class BackfillStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RawTransaction(Base):
    """Immutable audit log of raw transaction payloads.

    The primary key is a SHA256 content hash over the canonical
    business-relevant fields. If Comdirect ever corrects a booked
    transaction, the new payload hashes differently, gets a fresh row,
    and the previous row's `superseded_by` is pointed at the new hash.
    """

    __tablename__ = "raw_transactions"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    comdirect_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    superseded_by: Mapped[Optional[str]] = mapped_column(
        ForeignKey("raw_transactions.content_hash"), nullable=True
    )
    batch_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    type: Mapped[TypeEnum] = mapped_column(
        SQLEnum(TypeEnum, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )


class Budget(Base):
    __tablename__ = "budgets"

    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id"), primary_key=True
    )
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    regex_pattern: Mapped[str] = mapped_column(String, nullable=False)
    target_category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class NormalizedTransaction(Base):
    """Cleaned, deterministic transaction data.

    Single source of truth for Grafana and downstream agents. The
    primary key mirrors the active RawTransaction content-hash so that
    every normalized row is drillable back to the raw payload.
    """

    __tablename__ = "normalized_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw_content_hash: Mapped[str] = mapped_column(
        ForeignKey("raw_transactions.content_hash"), nullable=False, index=True
    )
    comdirect_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )

    booking_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False)

    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    sender: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    recipient: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sender_iban: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    recipient_iban: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    category_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("categories.id"), nullable=True
    )

    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_outlier: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    internal_transfer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Positive-amount transaction that *cancels* a prior expense (Krankenkassen-
    # Erstattung, Splitwise-Ausgleich, Arbeitgeber-Spesen, Amazon-Rückbuchung).
    # Excluded from income, folded into the *original* category's expenses so
    # budgets see net spend.
    is_refund: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Timestamp set when the user walks the manual refund-audit. Either a
    # row is reclassified as a refund (is_refund=True, category swapped) or
    # confirmed as real income (no other change) — both paths set this
    # timestamp so the audit endpoint stops re-surfacing the row.
    refund_audit_decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    recurring_pattern_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("recurring_patterns.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RecurringPattern(Base):
    """A detected recurring charge (e.g. Netflix, rent).

    Per the 2026-04-12 decision: a pattern exists when the same
    recipient appears in at least 3 consecutive months with amounts
    within ±10% of the mean.
    """

    __tablename__ = "recurring_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient: Mapped[str] = mapped_column(String, nullable=False, index=True)
    avg_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_stddev: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    first_seen_month: Mapped[date] = mapped_column(Date, nullable=False)
    last_seen_month: Mapped[date] = mapped_column(Date, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)


class TransactionTag(Base):
    __tablename__ = "transaction_tags"

    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_transactions.id"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id"), primary_key=True)


class AgentRun(Base):
    """Tracks individual agent runs triggered via the Runs API (M6).

    Each row represents one invocation of a named agent (e.g. weekly_report,
    anomaly_scan). The result column stores the agent's structured output.
    """

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[RunStatus] = mapped_column(
        SQLEnum(RunStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=RunStatus.PENDING,
    )
    trigger: Mapped[RunTrigger] = mapped_column(
        SQLEnum(RunTrigger, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=RunTrigger.MANUAL,
    )
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    progress_current: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    progress_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    usage_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Heartbeat: refreshed by _update_progress and at every batch boundary so a
    # stale-run reaper can distinguish a stalled run from one still making
    # progress. NULL on rows pre-dating M11 reliability work.
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Surfaced inline while status='running' when an internal batch fails
    # (e.g. transient LLM connection error). The run continues; the UI uses
    # this to render an amber "retrying" banner without ending the run.
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ReportStatus(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class Report(Base):
    """A generated financial report (monthly PDF, Markdown, etc.).

    Reports are produced by scheduled jobs and stored on disk; this table
    tracks metadata and the storage path so the API can list and serve them.
    """

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    format: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # pdf, md, html, json
    content: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[ReportStatus] = mapped_column(
        SQLEnum(ReportStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ReportStatus.PENDING,
    )
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SyncRun(Base):
    """Run record for raw ingestion and normalization passes.

    Prepared here so M7's Agent Orchestrator can reuse the same pattern
    without a schema change.
    """

    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[SyncSource] = mapped_column(
        SQLEnum(SyncSource, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    status: Mapped[SyncStatus] = mapped_column(
        SQLEnum(SyncStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SyncStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ReviewedSuggestion(Base):
    """Tracks transactions whose low-confidence categorization was reviewed.

    Currently only used to mark "rejected" suggestions so they stay out of
    the pending-review list on subsequent runs (the actual category, when
    accepted, ends up on `normalized_transactions.category_id` directly,
    which already filters them out via `category_id IS NULL`).
    """

    __tablename__ = "reviewed_suggestions"

    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_transactions.id"), primary_key=True
    )
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppSettings(Base):
    """User-tunable app settings (singleton row, id=1).

    Currently exposes the auto-apply confidence threshold for
    categorization and the default transaction-list page size.
    Add more knobs here when they need to be UI-editable rather than
    env-only.
    """

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    auto_apply_confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.60")
    )
    # Default rows-per-page for the transactions table. Bounds enforced
    # at the API layer (10..200) — the column itself stays loose so a
    # downstream migration can widen the range without a schema change.
    page_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=25, server_default="25"
    )
    # Optional Discord webhook URL — when set, the worker fires a
    # best-effort failure notification on FAILED sync/agent runs. Format
    # validation lives at the API layer (must start with the official
    # Discord webhook host); the column itself stays loose so future
    # providers (Slack, ntfy) can land without a schema change.
    webhook_url: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Portfolio / Depot (M8)
#
# Comdirect returns depot positions and transactions at every sync but has no
# history endpoint. `positions` is a per-sync snapshot (UNIQUE(depot_id, isin),
# refreshed every sync); `portfolio_snapshots` captures one row per depot + an
# aggregate row (depot_id NULL) per sync day so the UI can render a
# timeseries that grows over time.
# ---------------------------------------------------------------------------


class Depot(Base):
    __tablename__ = "depots"

    depot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    depot_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Instrument(Base):
    __tablename__ = "instruments"

    isin: Mapped[str] = mapped_column(String(12), primary_key=True)
    wkn: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    instrument_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    # Yahoo-Finance ticker for the M11 price-history backfill (e.g. ``SAP.DE``).
    # Set manually by the user via PATCH /portfolio/instruments/{isin} —
    # auto-mapping ISIN→ticker via yfinance is unreliable enough that we
    # leave it to the human.
    ticker_symbol: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class InstrumentPriceHistory(Base):
    """Cached daily close prices from an external provider (yfinance for now).

    One row per (instrument, day). Backfilled on demand via
    ``POST /portfolio/instruments/{isin}/backfill-prices`` and read by
    the UI for the depot-Rückblick chart. The unique constraint on
    (isin, price_date) makes repeated backfills idempotent.

    ``source`` is a free-form string (currently always ``"yfinance"``)
    so a future provider swap doesn't need a schema migration.
    """

    __tablename__ = "instrument_price_history"
    __table_args__ = (
        UniqueConstraint(
            "isin", "price_date", name="uq_instrument_price_history_isin_date"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    isin: Mapped[str] = mapped_column(
        ForeignKey("instruments.isin", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    close: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="yfinance")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Position(Base):
    """Current holdings per (depot, instrument). Refreshed on every sync."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("depot_id", "isin", name="uq_positions_depot_isin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    depot_id: Mapped[str] = mapped_column(
        ForeignKey("depots.depot_id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(
        ForeignKey("instruments.isin", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )
    current_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    purchase_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    prev_day_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 4), nullable=True
    )
    prev_day_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DepotTransactionType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    OTHER = "OTHER"


class DepotTransaction(Base):
    __tablename__ = "depot_transactions"

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    depot_id: Mapped[str] = mapped_column(
        ForeignKey("depots.depot_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    isin: Mapped[Optional[str]] = mapped_column(
        ForeignKey("instruments.isin", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    booking_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PortfolioSnapshot(Base):
    """Per-day snapshot of depot total value.

    `depot_id IS NULL` marks the aggregate row across all depots — the
    series the performance chart reads from.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "depot_id", "snapshot_date", name="uq_portfolio_snapshots_depot_date"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    depot_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("depots.depot_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    total_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_purchase_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(Base):
    """Single-user auth table (M10a).

    UUID PK to allow future multi-user migration (M17) without schema pain.
    No user_id FKs on transaction tables — that is M17 scope.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="admin")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BackfillRun(Base):
    """Tracks a historical-backfill walk over Comdirect account transactions.

    One row per user-initiated backfill (covers all accounts at once;
    ``account_id`` is intentionally NULL — per-account scoping is M17 work).
    The worker writes progress here so the UI can poll status.
    """

    __tablename__ = "backfill_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    target_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    current_window_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    windows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    windows_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[BackfillStatus] = mapped_column(
        SQLEnum(BackfillStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=BackfillStatus.RUNNING,
    )
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
