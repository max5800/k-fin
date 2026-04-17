"""Categories & Budgets router for the Finance API (M6)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import get_db, require_token
from src.api.schemas import BudgetOut, BudgetUpdate, CategoryCreate, CategoryOut
from src.core.db.models import Budget, Category, NormalizedTransaction, TypeEnum

router = APIRouter(
    prefix="/categories",
    tags=["categories"],
    dependencies=[Depends(require_token)],
)


# ── Categories ────────────────────────────────────────────────────────


@router.get("", response_model=list[CategoryOut])
def list_categories(db: Session = Depends(get_db)):
    rows = db.execute(select(Category).order_by(Category.name)).scalars().all()
    return [CategoryOut.model_validate(c) for c in rows]


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(body: CategoryCreate, db: Session = Depends(get_db)):
    valid_types = {t.value for t in TypeEnum}
    if body.type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid type '{body.type}'. Valid: {sorted(valid_types)}",
        )

    category_id = body.id or str(uuid.uuid4())
    clash = db.execute(
        select(Category).where(
            (Category.id == category_id) | (Category.name == body.name)
        )
    ).scalar_one_or_none()
    if clash:
        field = "id" if clash.id == category_id else "name"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Category with {field} '{getattr(clash, field)}' already exists",
        )

    category = Category(id=category_id, name=body.name, type=body.type)
    db.add(category)
    db.commit()
    db.refresh(category)
    return CategoryOut.model_validate(category)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: str, db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )
    # Unlink normalized transactions referencing this category
    txns = (
        db.execute(
            select(NormalizedTransaction).where(
                NormalizedTransaction.category_id == category_id
            )
        )
        .scalars()
        .all()
    )
    for txn in txns:
        txn.category_id = None
    # Remove associated budget
    budget = db.get(Budget, category_id)
    if budget:
        db.delete(budget)
    db.delete(category)
    db.commit()


# ── Budgets ───────────────────────────────────────────────────────────


@router.get("/budgets", response_model=list[BudgetOut])
def list_budgets(db: Session = Depends(get_db)):
    rows = db.execute(select(Budget)).scalars().all()
    result = []
    for b in rows:
        cat = db.get(Category, b.category_id)
        result.append(
            BudgetOut(
                category_id=b.category_id,
                monthly_limit=b.monthly_limit,
                currency=b.currency,
                category=CategoryOut.model_validate(cat) if cat else None,
            )
        )
    return result


@router.put("/budgets/{category_id}", response_model=BudgetOut)
def upsert_budget(
    category_id: str,
    body: BudgetUpdate,
    db: Session = Depends(get_db),
):
    cat = db.get(Category, category_id)
    if not cat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category '{category_id}' not found",
        )

    budget = db.get(Budget, category_id)
    if budget:
        budget.monthly_limit = body.monthly_limit
        budget.currency = body.currency
    else:
        budget = Budget(
            category_id=category_id,
            monthly_limit=body.monthly_limit,
            currency=body.currency,
        )
        db.add(budget)

    db.commit()
    db.refresh(budget)
    return BudgetOut(
        category_id=budget.category_id,
        monthly_limit=budget.monthly_limit,
        currency=budget.currency,
        category=CategoryOut.model_validate(cat),
    )
