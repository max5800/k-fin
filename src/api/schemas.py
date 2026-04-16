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
    id: str
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
    title: str
    period_start: date
    period_end: date
    format: str
    size_bytes: int | None
    status: str
    error: str | None
    created_at: datetime
    updated_at: datetime


class ReportListOut(BaseModel):
    items: list[ReportOut]
    total: int
    limit: int
    offset: int
