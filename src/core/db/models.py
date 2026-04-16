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

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String
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


class RunTrigger(str, enum.Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    WEBHOOK = "webhook"


class RawTransaction(Base):
    """Immutable audit log of raw transaction payloads.

    The primary key is a SHA256 content hash over the canonical
    business-relevant fields. If Comdirect ever corrects a booked
    transaction, the new payload hashes differently, gets a fresh row,
    and the previous row's `superseded_by` is pointed at the new hash.
    """

    __tablename__ = "raw_transactions"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    comdirect_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
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
    type: Mapped[TypeEnum] = mapped_column(SQLEnum(TypeEnum, values_callable=lambda e: [m.value for m in e]), nullable=False)


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
    target_category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), nullable=False)
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
    comdirect_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)

    booking_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False)

    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    sender: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    recipient: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sender_iban: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    recipient_iban: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    category_id: Mapped[Optional[str]] = mapped_column(ForeignKey("categories.id"), nullable=True)

    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_outlier: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    internal_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

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
        SQLEnum(RunStatus, values_callable=lambda e: [m.value for m in e]), nullable=False, default=RunStatus.PENDING
    )
    trigger: Mapped[RunTrigger] = mapped_column(
        SQLEnum(RunTrigger, values_callable=lambda e: [m.value for m in e]), nullable=False, default=RunTrigger.MANUAL
    )
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
    title: Mapped[str] = mapped_column(String, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False)  # pdf, md, html
    file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[ReportStatus] = mapped_column(
        SQLEnum(ReportStatus, values_callable=lambda e: [m.value for m in e]), nullable=False, default=ReportStatus.PENDING
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
        SQLEnum(SyncStatus, values_callable=lambda e: [m.value for m in e]), nullable=False, default=SyncStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
