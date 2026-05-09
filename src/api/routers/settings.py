"""App-settings endpoints — singleton config editable from the UI."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import Auth, get_db
from src.core.db.models import AppSettings

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Auth])

# Hard cap mirrors the transactions-list `limit` ceiling and prevents the
# UI from saving a setting it can't actually use against the API.
PAGE_SIZE_MIN = 10
PAGE_SIZE_MAX = 200
PAGE_SIZE_DEFAULT = 25


class SettingsOut(BaseModel):
    auto_apply_confidence: float
    page_size: int

    @classmethod
    def from_row(cls, row: AppSettings) -> "SettingsOut":
        return cls(
            auto_apply_confidence=float(row.auto_apply_confidence),
            page_size=row.page_size,
        )


class SettingsUpdate(BaseModel):
    # Both fields optional so the UI can update one knob at a time without
    # resending the other (None means "leave as is").
    auto_apply_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    page_size: int | None = Field(
        default=None, ge=PAGE_SIZE_MIN, le=PAGE_SIZE_MAX
    )


def _get_or_create(db: Session) -> AppSettings:
    row = db.get(AppSettings, 1)
    if row is None:
        row = AppSettings(
            id=1,
            auto_apply_confidence=Decimal("0.60"),
            page_size=PAGE_SIZE_DEFAULT,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("", response_model=SettingsOut)
def read_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return SettingsOut.from_row(_get_or_create(db))


@router.put("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate, db: Session = Depends(get_db)
) -> SettingsOut:
    row = _get_or_create(db)
    if payload.auto_apply_confidence is not None:
        try:
            row.auto_apply_confidence = Decimal(str(payload.auto_apply_confidence))
        except (ValueError, TypeError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid auto_apply_confidence: {e}",
            )
    if payload.page_size is not None:
        row.page_size = payload.page_size
    db.commit()
    db.refresh(row)
    return SettingsOut.from_row(row)
