"""Pydantic schemas for transaction endpoints."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.api.schemas.common import PaginatedResponse


def _mask_iban(iban: str | None) -> str | None:
    if not iban or len(iban) < 8:
        return iban
    return iban[:4] + "****" + iban[-4:]


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    comdirect_id: str | None
    booking_date: date
    valuation_date: date
    amount: Decimal
    currency: str
    sender: str | None
    recipient: str | None
    sender_iban: str | None = None
    recipient_iban: str | None = None
    description: str | None
    category_id: str | None
    category_name: str | None = None
    category_type: str | None = None
    tags: list[str] = []
    is_recurring: bool
    is_outlier: bool
    internal_transfer: bool
    recurring_pattern_id: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_masked(
        cls, txn, *, include_ibans: bool = False
    ) -> "TransactionResponse":
        category_name = txn.category.name if txn.category else None
        category_type = txn.category.type.value if txn.category else None
        tag_names = [ta.tag_id for ta in txn.tag_associations]

        data = {
            "id": txn.id,
            "comdirect_id": txn.comdirect_id,
            "booking_date": txn.booking_date,
            "valuation_date": txn.valuation_date,
            "amount": txn.amount,
            "currency": txn.currency,
            "sender": txn.sender,
            "recipient": txn.recipient,
            "sender_iban": (
                txn.sender_iban if include_ibans else _mask_iban(txn.sender_iban)
            ),
            "recipient_iban": (
                txn.recipient_iban if include_ibans else _mask_iban(txn.recipient_iban)
            ),
            "description": txn.description,
            "category_id": txn.category_id,
            "category_name": category_name,
            "category_type": category_type,
            "tags": tag_names,
            "is_recurring": txn.is_recurring,
            "is_outlier": txn.is_outlier,
            "internal_transfer": txn.internal_transfer,
            "recurring_pattern_id": txn.recurring_pattern_id,
            "created_at": txn.created_at,
            "updated_at": txn.updated_at,
        }
        return cls(**data)


class TransactionListResponse(PaginatedResponse):
    items: list[TransactionResponse]


class UpdateCategoryRequest(BaseModel):
    category_id: str | None


class AddTagRequest(BaseModel):
    tag_id: str
