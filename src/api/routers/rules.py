"""Categorization-rules CRUD + apply-all endpoint.

Mounted under ``/categories/rules`` so the existing UI client (k-fin-ui
``RulesSection.tsx``) finds the endpoints at the path it already calls.
The router is its own module (rather than living in
``categories.py``) because it owns a non-trivial surface — CRUD plus
``apply-all`` — and shares the rule-matching primitives with the
normalization pipeline via ``src.normalization.pipeline``.

Auth follows the same convention as other write-capable routers: the
shared ``require_token`` dependency, which accepts either a valid JWT
(browser) or the static ``API_TOKEN`` (service callers).
"""

from __future__ import annotations

import logging
import re

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Response,
    status,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import RuleCreate, RuleOut, RulesApplyResult, RuleUpdate
from src.core.db.models import Category, NormalizedTransaction, Rule
from src.normalization.pipeline import (
    build_rule_haystack,
    match_rule,
    sort_rules_by_priority,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/categories/rules",
    tags=["categories", "rules"],
    dependencies=[Depends(require_token)],
)


# Above this many uncategorised transactions, apply-all returns 202 and
# offloads the scan to a background task so the request doesn't block on
# what is essentially a regex sweep over the whole DB. The figure is
# tuned for the canonical single-user dataset (~5–10 yrs of transactions
# = a few thousand rows) — the worst-case sweep over 5k rows with ~30
# rules ran in <1.5 s in local benchmarks; the threshold is the cliff
# at which we stop being confident the request fits inside the
# fronting reverse-proxy's read timeout.
_APPLY_ALL_SYNC_LIMIT = 1000


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
    rules are tried during apply-all.
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


# ── Apply-all ─────────────────────────────────────────────────────────


def _apply_rules_to_uncategorised(session: Session) -> RulesApplyResult:
    """Run all rules over uncategorised transactions in one pass.

    Reuses the pipeline's rule-matching primitives so backend behaviour
    is identical to the regular normalization run. Only rows with
    ``category_id IS NULL`` are touched — once a row has a category we
    treat that as a deliberate decision (agent, manual edit, or a
    previous rule application) and never overwrite it. This mirrors the
    `coalesce(category_id)` guard in the upsert path.
    """
    rules = session.execute(select(Rule)).scalars().all()
    rules_sorted = sort_rules_by_priority(rules)

    rows = (
        session.execute(
            select(NormalizedTransaction).where(
                NormalizedTransaction.category_id.is_(None)
            )
        )
        .scalars()
        .all()
    )

    processed = len(rows)
    matched = 0

    if not rules_sorted or not rows:
        return RulesApplyResult(
            status="completed",
            processed=processed,
            matched=0,
            unchanged=processed,
        )

    for tx in rows:
        haystack = build_rule_haystack(
            sender=tx.sender, recipient=tx.recipient, description=tx.description
        )
        rule = match_rule(rules_sorted, haystack)
        if rule is None:
            continue
        session.execute(
            update(NormalizedTransaction)
            .where(NormalizedTransaction.id == tx.id)
            .values(category_id=rule.target_category_id)
        )
        matched += 1

    session.commit()
    return RulesApplyResult(
        status="completed",
        processed=processed,
        matched=matched,
        unchanged=processed - matched,
    )


def _run_apply_all_in_background(database_url: str) -> None:
    """Background task: open a fresh session and run apply-all.

    The request-scoped ``Session`` from FastAPI's dependency is closed
    by the time this runs, so we build our own engine + session.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SqlSession

    engine = create_engine(database_url)
    try:
        with SqlSession(engine) as session:
            result = _apply_rules_to_uncategorised(session)
        logger.info(
            "rules apply-all (background): processed=%d matched=%d unchanged=%d",
            result.processed,
            result.matched,
            result.unchanged,
        )
    except Exception:  # noqa: BLE001 — log and move on; nothing else to do
        logger.exception("rules apply-all background task failed")
    finally:
        engine.dispose()


@router.post("/apply-all", response_model=RulesApplyResult)
def apply_all_rules(
    response: Response,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RulesApplyResult:
    """Apply all rules to every uncategorised transaction.

    Synchronous below ``_APPLY_ALL_SYNC_LIMIT`` rows (returns 200 with
    the counts). Above that, schedules a background task and returns
    202 Accepted with ``status="accepted"`` and ``processed`` reflecting
    the size of the work queued. The UI listens for the 202 and falls
    back to polling ``GET /categories/rules`` + the transaction list to
    learn the outcome.
    """
    pending_count = db.execute(
        select(func.count())
        .select_from(NormalizedTransaction)
        .where(NormalizedTransaction.category_id.is_(None))
    ).scalar_one()

    if pending_count > _APPLY_ALL_SYNC_LIMIT:
        from src.core.config import settings

        if not settings.database_url:
            # Fall back to synchronous if we can't construct a worker
            # engine — better to time out honestly than to silently
            # never run.
            return _apply_rules_to_uncategorised(db)

        background_tasks.add_task(
            _run_apply_all_in_background, settings.database_url
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return RulesApplyResult(status="accepted", processed=pending_count)

    return _apply_rules_to_uncategorised(db)
