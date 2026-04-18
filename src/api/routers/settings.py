"""App-settings endpoints — singleton config editable from the UI."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import Auth, get_db
from src.core.db.models import AppSettings

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Auth])


class SettingsOut(BaseModel):
    auto_apply_confidence: float

    @classmethod
    def from_row(cls, row: AppSettings) -> "SettingsOut":
        return cls(auto_apply_confidence=float(row.auto_apply_confidence))


class SettingsUpdate(BaseModel):
    auto_apply_confidence: float = Field(ge=0.0, le=1.0)


def _get_or_create(db: Session) -> AppSettings:
    row = db.get(AppSettings, 1)
    if row is None:
        row = AppSettings(id=1, auto_apply_confidence=Decimal("0.60"))
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
    try:
        row.auto_apply_confidence = Decimal(str(payload.auto_apply_confidence))
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid auto_apply_confidence: {e}",
        )
    db.commit()
    db.refresh(row)
    return SettingsOut.from_row(row)
