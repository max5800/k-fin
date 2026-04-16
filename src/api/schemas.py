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
