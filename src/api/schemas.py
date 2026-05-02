"""Pydantic schemas for the Finance API (M6)."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str


class CategoryCreate(BaseModel):
    id: str | None = None
    name: str
    type: str


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category_id: str
    monthly_limit: Decimal
    currency: str
    category: CategoryOut | None = None


class BudgetUpdate(BaseModel):
    monthly_limit: Decimal
    currency: str = "EUR"


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class TagCreate(BaseModel):
    id: str | None = None
    name: str


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    comdirect_id: str | None
    booking_date: date
    valuation_date: date
    amount: Decimal
    currency: str
    sender: str | None
    recipient: str | None
    description: str | None
    category: CategoryOut | None = None
    tags: list[TagOut] = []
    is_recurring: bool
    is_outlier: bool
    internal_transfer: bool
    created_at: datetime
    updated_at: datetime


class TransactionListOut(BaseModel):
    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


class TransactionUpdate(BaseModel):
    category_id: str | None = None
    tags: list[str] | None = None


# ── Runs API (M6) ────────────────────────────────────────────────


class RunCreate(BaseModel):
    trigger: str = "manual"


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_name: str
    status: str
    trigger: str
    result: dict | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_message: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    usage_detail: dict | None = None
    # M11 reliability surface — surfaced live while status='running'.
    heartbeat_at: datetime | None = None
    last_error: str | None = None


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


class CategoryBreakdown(BaseModel):
    category_id: str
    category_name: str
    total: Decimal
    transaction_count: int


class MonthlySummaryOut(BaseModel):
    year: int
    month: int
    income: Decimal
    expenses: Decimal
    net: Decimal
    # Savings rate as a fraction in [-inf, 1.0]: 0.30 means 30 %.
    # API convention: all `_rate` / `_ratio` fields are fractions, never percent values.
    savings_rate: Decimal
    transaction_count: int
    by_category: list[CategoryBreakdown]


class CashflowPoint(BaseModel):
    year: int
    month: int
    income: Decimal
    expenses: Decimal
    net: Decimal
    transaction_count: int


class CashflowOverTimeOut(BaseModel):
    series: list[CashflowPoint]
    total_months: int


# ── Reports API (M6) ────────────────────────────────────────────


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    report_type: str
    title: str
    period_start: date
    period_end: date
    format: str
    file_path: str | None
    size_bytes: int | None
    status: str
    error: str | None
    # Agent-produced JSON payload (CategorizationResult, AnalysisResult, …).
    # NULL for legacy/file-backed PDF/MD reports.
    content: dict | None = None
    created_at: datetime
    updated_at: datetime


class ReportListOut(BaseModel):
    items: list[ReportOut]
    total: int
    limit: int
    offset: int


# ── Portfolio / Depot (M8) ─────────────────────────────────────


class DepotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    depot_id: str
    depot_type: str | None = None
    currency: str
    total_value: Decimal
    total_purchase_value: Decimal
    total_pnl_abs: Decimal
    total_pnl_rel: Decimal
    positions_count: int
    last_synced_at: datetime | None = None


class InstrumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    isin: str
    wkn: str | None = None
    name: str
    instrument_type: str | None = None
    currency: str


class PositionOut(BaseModel):
    depot_id: str
    instrument: InstrumentOut
    quantity: Decimal
    current_price: Decimal
    current_value: Decimal
    purchase_value: Decimal
    prev_day_price: Decimal | None = None
    daily_pnl_abs: Decimal
    daily_pnl_rel: Decimal
    total_pnl_abs: Decimal
    total_pnl_rel: Decimal
    weight_pct: Decimal
    currency: str
    as_of: datetime


class DepotTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    depot_id: str
    isin: str | None = None
    booking_date: date
    transaction_type: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    currency: str


class DepotTransactionListOut(BaseModel):
    items: list[DepotTransactionOut]
    total: int
    limit: int
    offset: int


class PortfolioSummaryOut(BaseModel):
    total_value: Decimal
    total_purchase_value: Decimal
    total_pnl_abs: Decimal
    total_pnl_rel: Decimal
    daily_pnl_abs: Decimal
    daily_pnl_rel: Decimal
    dividend_yield_pct: Decimal
    positions_count: int
    depots_count: int
    last_synced_at: datetime | None = None


class AllocationBucketOut(BaseModel):
    bucket: str
    value: Decimal
    share_pct: Decimal


class PerformancePointOut(BaseModel):
    snapshot_date: date
    total_value: Decimal
    total_purchase_value: Decimal
