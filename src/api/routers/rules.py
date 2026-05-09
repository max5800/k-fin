"""Categorization-rules CRUD router.

Mounted under ``/categories/rules`` so the existing UI client (k-fin-ui
``RulesSection.tsx``) finds the endpoints at the path it already calls.
The router is its own module (rather than living in ``categories.py``)
because rules form a coherent surface that will keep growing —
apply-all, preview, and the per-rule match counter all land here.

Auth follows the same convention as other write-capable routers: the
shared ``require_token`` dependency, which accepts either a valid JWT
(browser) or the static ``API_TOKEN`` (service callers).
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import RuleCreate, RuleOut, RuleUpdate
from src.core.db.models import Category, Rule

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/categories/rules",
    tags=["categories", "rules"],
    dependencies=[Depends(require_token)],
)


# ── Helpers ───────────────────────────────────────────────────────────


def _validate_regex(pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid regex pattern: {exc}",
        )


def _validate_category_exists(db: Session, category_id: str) -> None:
    if not db.get(Category, category_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Category '{category_id}' not found",
        )


# ── CRUD ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[RuleOut])
def list_rules(db: Session = Depends(get_db)) -> list[RuleOut]:
    """Return all rules sorted by descending priority, then by id.

    The UI sorts again client-side for cosmetic stability, but the
    server is the source of truth — the same order matches the order
    rules are tried during the normalization pipeline.
    """
    rows = (
        db.execute(select(Rule).order_by(Rule.priority.desc(), Rule.id.asc()))
        .scalars()
        .all()
    )
    return [RuleOut.model_validate(r) for r in rows]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleCreate, db: Session = Depends(get_db)) -> RuleOut:
    _validate_regex(body.regex_pattern)
    _validate_category_exists(db, body.target_category_id)

    rule = Rule(
        regex_pattern=body.regex_pattern,
        target_category_id=body.target_category_id,
        priority=body.priority,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.patch("/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: int, body: RuleUpdate, db: Session = Depends(get_db)
) -> RuleOut:
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found"
        )

    if body.regex_pattern is not None:
        _validate_regex(body.regex_pattern)
        rule.regex_pattern = body.regex_pattern
    if body.target_category_id is not None:
        _validate_category_exists(db, body.target_category_id)
        rule.target_category_id = body.target_category_id
    if body.priority is not None:
        rule.priority = body.priority

    db.commit()
    db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> None:
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found"
        )
    db.delete(rule)
    db.commit()
