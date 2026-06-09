"""Mail evidence endpoints.

These routes accept mock/structured mail input for now. A real Gmail connector
should feed the same service layer later; raw mail content is never persisted.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.api.deps import Auth, get_db
from src.api.schemas import (
    EvidenceLinkOut,
    MailEvidenceImportOut,
    MailEvidenceOut,
    MailMessageImport,
)
from src.core.db.models import MailEvidence
from src.services.mail_evidence import import_mail_message, match_evidence_to_transactions

router = APIRouter(prefix="/mail-evidence", tags=["mail-evidence"], dependencies=[Auth])


@router.get("", response_model=list[MailEvidenceOut])
def list_mail_evidence(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    evidence_type: str | None = None,
) -> list[MailEvidenceOut]:
    stmt = select(MailEvidence).order_by(MailEvidence.document_date.desc().nullslast())
    if evidence_type:
        stmt = stmt.where(MailEvidence.evidence_type == evidence_type)
    rows = db.execute(stmt.limit(limit).offset(offset)).scalars().all()
    return [MailEvidenceOut.model_validate(row) for row in rows]


@router.post("/mock-import", response_model=MailEvidenceImportOut)
def import_mock_mail_evidence(
    body: MailMessageImport,
    db: Session = Depends(get_db),
) -> MailEvidenceImportOut:
    evidence, links = import_mail_message(db, body.model_dump())
    return MailEvidenceImportOut(
        evidence=MailEvidenceOut.model_validate(evidence),
        links=[EvidenceLinkOut.model_validate(link) for link in links],
    )


@router.post("/{evidence_id}/match", response_model=list[EvidenceLinkOut])
def rematch_mail_evidence(
    evidence_id: str,
    db: Session = Depends(get_db),
) -> list[EvidenceLinkOut]:
    evidence = db.get(MailEvidence, evidence_id)
    if evidence is None:
        return []
    links = match_evidence_to_transactions(db, evidence)
    db.commit()
    return [EvidenceLinkOut.model_validate(link) for link in links]
