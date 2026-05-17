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
re-normalizes, carries the user-set category / refund flags onto the
fresh normalized row, and drops the orphaned old normalized rows.

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

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db.models import DataSource, NormalizedTransaction, RawTransaction
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

    # --- carry user-set fields off the soon-to-be-superseded rows -----
    carry: dict[str, dict] = {}
    with Session(engine) as session:
        for old_hash, new_hash in transitions:
            old = session.get(NormalizedTransaction, old_hash)
            if old is not None:
                carry[new_hash] = {
                    "category_id": old.category_id,
                    "is_refund": old.is_refund,
                    "refund_audit_decided_at": old.refund_audit_decided_at,
                }

    # --- re-ingest as new versions, then re-normalize -----------------
    pipeline = NormalizationPipeline(
        settings.database_url, own_ibans=settings.get_own_ibans()
    )
    inserted = pipeline.load_raw_transactions(reingest)
    print(f"Re-ingested {inserted} row(s) as new versions (old rows superseded).")

    _, run_id = pipeline.process_and_normalize()
    print(f"Re-normalized — sync run {run_id}.")

    # --- carry the user-set fields onto the fresh normalized rows -----
    carried = 0
    with Session(engine) as session:
        for new_hash, fields in carry.items():
            new = session.get(NormalizedTransaction, new_hash)
            if new is None:
                continue
            # A non-null prior category is the user's / agent's choice —
            # it wins over whatever rule matching just assigned.
            if fields["category_id"]:
                new.category_id = fields["category_id"]
            new.is_refund = fields["is_refund"]
            new.refund_audit_decided_at = fields["refund_audit_decided_at"]
            carried += 1
        session.commit()
    print(f"Carried category / refund flags onto {carried} re-ingested row(s).")

    # --- drop the orphaned old normalized rows ------------------------
    # _build_dataframe only normalizes active raw rows, so a normalized
    # row backed by a now-superseded raw row is an orphan.
    with Session(engine) as session:
        superseded = select(RawTransaction.content_hash).where(
            RawTransaction.superseded_by.isnot(None)
        )
        deleted = session.execute(
            NormalizedTransaction.__table__.delete().where(
                NormalizedTransaction.raw_content_hash.in_(superseded)
            )
        ).rowcount
        session.commit()
    print(f"Removed {deleted} orphaned normalized row(s).")
    print("\nDone — Comdirect credits now carry the remitter IBAN as sender_iban.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
