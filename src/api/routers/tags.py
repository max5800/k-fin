"""Tag endpoints — list, create, delete."""

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from src.api.deps import Auth, Db
from src.api.schemas.tags import TagCreate, TagResponse
from src.core.db.models import Tag, TransactionTag

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("", response_model=list[TagResponse])
def list_tags(db: Db, _auth: None = Auth):
    tags = db.scalars(select(Tag).order_by(Tag.name)).all()
    return [TagResponse.model_validate(t) for t in tags]


@router.post("", response_model=TagResponse, status_code=201)
def create_tag(body: TagCreate, db: Db, _auth: None = Auth):
    existing = db.get(Tag, body.id)
    if existing:
        raise HTTPException(status_code=409, detail="Tag with this ID already exists")
    tag = Tag(id=body.id, name=body.name)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.delete("/{tag_id}", status_code=204)
def delete_tag(tag_id: str, db: Db, _auth: None = Auth):
    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    # Remove all associations first
    assocs = db.scalars(
        select(TransactionTag).where(TransactionTag.tag_id == tag_id)
    ).all()
    for a in assocs:
        db.delete(a)
    db.delete(tag)
    db.commit()
