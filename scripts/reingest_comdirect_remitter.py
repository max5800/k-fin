#!/usr/bin/env python
"""Re-ingest Comdirect raw transactions after the remitter-mapping fix.

Background
----------
Until the ``feat/comdirect-eigentransfer`` fix, ``ComdirectTransaction``
read the incoming counterparty from a ``debtor`` key Comdirect never
sends — the credit-side counterparty arrives under ``remitter`` (its
``deptor`` field, sic, is always empty). Every credit was therefore
stored with empty ``debtor_name`` / ``debtor_iban``, so own-account
transfers carried no sender IBAN and stayed invisible to internal-
transfer detection.

The code fix is in ``src/external/models.py``. Rows already in the DB
keep the old (broken) flat parse, but their ``raw_data.source_payload``
holds the complete upstream API dict — ``remitter`` included.

What this does
--------------
Re-parses every active Comdirect raw transaction from its
``source_payload`` through the corrected model and recomputes the
content hash. For a row whose canonical content changed (a credit that
gains a sender name / IBAN) it re-ingests the row as a new version — the
old row is superseded, exactly as for a Comdirect correction — then
re-normalizes and carries the user-set category / refund flags onto the
fresh normalized row. The predecessor and its transaction-link history remain
as inactive audit records.

No Comdirect API access: runs entirely off the stored payloads.
Idempotent — a second run finds nothing to change.

Usage
-----
    uv run python scripts/reingest_comdirect_remitter.py            # dry run
    uv run python scripts/reingest_comdirect_remitter.py --apply    # mutate

Needs ``DATABASE_URL`` pointing at the target database.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    ReviewedSuggestion,
    SubscriptionRecord,
    TransactionEvidenceLink,
    TransactionTag,
    ValueAssessment,
)
from src.external.models import ComdirectTransaction
from src.normalization.canonicalize import canonicalize, content_hash
from src.normalization.pipeline import NormalizationPipeline


def _rehash(raw: RawTransaction) -> tuple[str, dict] | None:
    """Re-parse a Comdirect raw row from its ``source_payload`` through the
    corrected model. Returns ``(new_hash, new_raw_data)`` when the content
    hash changed, else ``None`` (unchanged, or a legacy row with no payload)."""
    source_payload = (raw.raw_data or {}).get("source_payload")
    if not isinstance(source_payload, dict) or not source_payload:
        return None
    new_raw_data = ComdirectTransaction.model_validate(source_payload).model_dump()
    new_hash = content_hash(canonicalize(new_raw_data), source="comdirect")
    if new_hash == raw.content_hash:
        return None
    return new_hash, new_raw_data


def _collect_carry_fields(
    session: Session, transitions: list[tuple[str, str]]
) -> dict[str, dict]:
    """Snapshot semantics from active predecessors before normalization."""
    carry: dict[str, dict] = {}
    for old_hash, new_hash in transitions:
        old = session.execute(
            select(NormalizedTransaction)
            .where(NormalizedTransaction.raw_content_hash == old_hash)
            .where(NormalizedTransaction.is_active.is_(True))
            .with_for_update()
        ).scalar_one_or_none()
        if old is not None:
            carry[new_hash] = {
                "predecessor_id": old.id,
                "category_id": old.category_id,
                "is_refund": old.is_refund,
                "refund_verification_status": old.refund_verification_status,
                "refund_audit_decided_at": old.refund_audit_decided_at,
            }
    return carry


def _carry_reference_graph(
    session: Session, *, predecessor_id: str, successor_id: str
) -> None:
    """Make predecessor decisions/evidence reachable from the active revision.

    Association rows are copied so the inactive revision remains a complete
    audit record. Owned evidence rows are moved without changing their identity
    or actor attribution. Every operation is duplicate-safe for a transaction
    retry; conflicting value assessments fail closed instead of overwriting a
    user decision.
    """
    predecessor_tags = session.execute(
        select(TransactionTag)
        .where(TransactionTag.transaction_id == predecessor_id)
        .with_for_update()
    ).scalars()
    for predecessor_tag in predecessor_tags:
        key = (successor_id, predecessor_tag.tag_id)
        if session.get(TransactionTag, key) is None:
            session.add(
                TransactionTag(transaction_id=successor_id, tag_id=predecessor_tag.tag_id)
            )

    predecessor_review = session.execute(
        select(ReviewedSuggestion)
        .where(ReviewedSuggestion.transaction_id == predecessor_id)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        predecessor_review is not None
        and session.get(ReviewedSuggestion, successor_id) is None
    ):
        session.add(
            ReviewedSuggestion(
                transaction_id=successor_id,
                reason=predecessor_review.reason,
                decided_at=predecessor_review.decided_at,
            )
        )

    predecessor_evidence = session.execute(
        select(TransactionEvidenceLink)
        .where(TransactionEvidenceLink.transaction_id == predecessor_id)
        .with_for_update()
    ).scalars()
    for link in predecessor_evidence:
        duplicate = session.execute(
            select(TransactionEvidenceLink.id).where(
                TransactionEvidenceLink.transaction_id == successor_id,
                TransactionEvidenceLink.evidence_id == link.evidence_id,
                TransactionEvidenceLink.match_type == link.match_type,
            )
        ).scalar_one_or_none()
        if duplicate is None:
            link_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "k-fin:transaction-evidence-link:"
                    f"{successor_id}:{link.evidence_id}:{link.match_type}",
                )
            )
            session.add(
                TransactionEvidenceLink(
                    id=link_id,
                    transaction_id=successor_id,
                    evidence_id=link.evidence_id,
                    match_type=link.match_type,
                    confidence=link.confidence,
                    match_reason=link.match_reason,
                    created_at=link.created_at,
                )
            )

    subscriptions = session.execute(
        select(SubscriptionRecord)
        .where(SubscriptionRecord.transaction_id == predecessor_id)
        .with_for_update()
    ).scalars()
    for subscription in subscriptions:
        subscription.transaction_id = successor_id

    predecessor_assessment = session.execute(
        select(ValueAssessment)
        .where(ValueAssessment.transaction_id == predecessor_id)
        .with_for_update()
    ).scalar_one_or_none()
    if predecessor_assessment is not None:
        successor_assessment = session.execute(
            select(ValueAssessment)
            .where(ValueAssessment.transaction_id == successor_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            successor_assessment is not None
            and successor_assessment is not predecessor_assessment
        ):
            raise RuntimeError(
                "remitter re-ingest cannot merge conflicting value assessments"
            )
        predecessor_assessment.transaction_id = successor_id


def _enforce_raw_supersession(
    session: Session, transitions: list[tuple[str, str]]
) -> None:
    """Persist each planned predecessor/successor edge explicitly.

    Generic ingest versioning discovers predecessors through ``external_id``.
    That column is nullable for legacy rows, while this maintenance pass already
    has the exact content-hash transitions.  Use those authoritative edges so a
    null legacy identifier cannot leave both raw and normalized revisions active.
    """
    for old_hash, new_hash in transitions:
        predecessor = session.execute(
            select(RawTransaction)
            .where(RawTransaction.content_hash == old_hash)
            .with_for_update()
        ).scalar_one_or_none()
        successor = session.get(RawTransaction, new_hash)
        if predecessor is None or successor is None:
            raise RuntimeError("remitter re-ingest transition is incomplete")
        if predecessor.superseded_by not in {None, new_hash}:
            raise RuntimeError("remitter re-ingest predecessor already has another successor")
        predecessor.superseded_by = new_hash
        successor.version = max(successor.version, predecessor.version + 1)


def _carry_fields_and_refresh_accounting(
    session: Session, carry: dict[str, dict], *, commit: bool = True
) -> int:
    """Copy predecessor semantics and classify successors in one transaction."""
    carried = 0
    for new_hash, fields in carry.items():
        successor = session.execute(
            select(NormalizedTransaction)
            .where(NormalizedTransaction.raw_content_hash == new_hash)
            .where(NormalizedTransaction.is_active.is_(True))
        ).scalar_one_or_none()
        if successor is None:
            continue
        # A non-null predecessor category represents an explicit/rule decision
        # and wins over a category assigned during this normalization pass.
        if fields["category_id"]:
            successor.category_id = fields["category_id"]
        successor.is_refund = fields["is_refund"]
        successor.refund_verification_status = fields[
            "refund_verification_status"
        ]
        successor.refund_audit_decided_at = fields["refund_audit_decided_at"]
        _carry_reference_graph(
            session,
            predecessor_id=fields["predecessor_id"],
            successor_id=successor.id,
        )
        carried += 1

    # Do not commit inside the refresh: copied semantics and their derived
    # accounting interpretation must become visible atomically.
    NormalizationPipeline._refresh_accounting_classification(session, commit=False)
    if commit:
        session.commit()
    return carried


def _reingest_atomically(
    pipeline: NormalizationPipeline,
    reingest: list[dict],
    transitions: list[tuple[str, str]],
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> tuple[int, str, int]:
    """Apply the full remitter repair in one database transaction.

    ``failure_hook`` is intentionally test-only: raising at any named stage
    proves that raw versioning, normalized successors, links, SyncRun state,
    and copied user semantics roll back together.
    """

    def checkpoint(stage: str) -> None:
        if failure_hook is not None:
            failure_hook(stage)

    with Session(pipeline.engine) as session, session.begin():
        carry = _collect_carry_fields(session, transitions)
        inserted = pipeline.load_raw_transactions(
            reingest, session=session, commit=False
        )
        _enforce_raw_supersession(session, transitions)
        checkpoint("after_raw_supersession")
        _frame, run_id = pipeline.process_and_normalize(
            session=session, commit=False
        )
        checkpoint("after_normalization")
        carried = _carry_fields_and_refresh_accounting(
            session, carry, commit=False
        )
        checkpoint("after_semantic_carryover")
    return inserted, run_id, carried


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate the database (default: dry-run report only).",
    )
    args = parser.parse_args()

    if not settings.database_url:
        print("DATABASE_URL not configured.", file=sys.stderr)
        return 2

    engine = create_engine(settings.database_url)

    # --- scan: which active Comdirect rows re-parse to a new hash? ----
    with Session(engine) as session:
        rows = (
            session.execute(
                select(RawTransaction).where(
                    RawTransaction.source == DataSource.COMDIRECT,
                    RawTransaction.superseded_by.is_(None),
                )
            )
            .scalars()
            .all()
        )
        legacy = 0
        reingest: list[dict] = []
        transitions: list[tuple[str, str]] = []  # (old_hash, new_hash)
        for raw in rows:
            sp = (raw.raw_data or {}).get("source_payload")
            if not isinstance(sp, dict) or not sp:
                legacy += 1
                continue
            result = _rehash(raw)
            if result is None:
                continue
            new_hash, new_raw_data = result
            reingest.append(
                {
                    "content_hash": new_hash,
                    "raw_data": new_raw_data,
                    "source": DataSource.COMDIRECT,
                    "external_id": raw.external_id,
                    "batch_id": raw.batch_id,
                }
            )
            transitions.append((raw.content_hash, new_hash))

    print(f"Comdirect active raw rows scanned    : {len(rows)}")
    if legacy:
        print(f"  legacy rows without source_payload (skipped): {legacy}")
    print(f"Rows whose canonical content changed : {len(reingest)}")

    if not reingest:
        print("Nothing to re-ingest — the data is already up to date.")
        return 0

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to re-ingest.")
        return 0

    # --- one transaction: raw version → normalize → semantic carry ----
    pipeline = NormalizationPipeline(settings.database_url)
    inserted, run_id, carried = _reingest_atomically(
        pipeline, reingest, transitions
    )
    print(f"Re-ingested {inserted} row(s) as new versions (old rows superseded).")
    print(f"Re-normalized — sync run {run_id}.")
    print(f"Carried category / refund flags onto {carried} re-ingested row(s).")
    print("Preserved superseded normalized rows and transaction-link audit history.")
    print("\nDone — Comdirect credits now carry the remitter IBAN as sender_iban.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
