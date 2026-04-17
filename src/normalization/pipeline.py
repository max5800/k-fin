"""Normalization pipeline: raw payloads → tagged, deduped transactions.

Idempotency contract:
- Identical raw payloads produce identical content hashes → skipped on re-ingest.
- Comdirect-corrected payloads produce a new hash → inserted as version=old+1,
  old row's `superseded_by` is pointed at the new hash. No data is lost.

Only raw rows with `superseded_by IS NULL` flow into normalized_transactions.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import create_engine, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.core.db.models import (
    NormalizedTransaction,
    RawTransaction,
    RecurringPattern,
    Rule,
    SyncRun,
    SyncSource,
    SyncStatus,
)
from src.normalization.canonicalize import canonicalize

logger = logging.getLogger(__name__)

INTERNAL_TRANSFER_DAY_WINDOW = 2
RECURRING_MIN_MONTHS = 3
RECURRING_AMOUNT_TOLERANCE = Decimal("0.10")  # ±10 %
# Modified z-score threshold (Iglewicz & Hoaglin, 1993). Robust against
# masking by extreme values — a property mean/stddev-based z-scores lack
# at small sample sizes.
OUTLIER_MODIFIED_Z_THRESHOLD = 3.5


class NormalizationPipeline:
    def __init__(self, database_url: str, own_ibans: Iterable[str] | None = None):
        self.engine = create_engine(database_url)
        self.own_ibans = {i for i in (own_ibans or []) if i}

    # ------------------------------------------------------------------
    # Raw ingest with content-hash versioning
    # ------------------------------------------------------------------

    def load_raw_transactions(self, rows: list[dict[str, Any]]) -> int:
        """Insert raw rows with versioning.

        Each row dict must provide: `content_hash`, `raw_data`, and
        optionally `comdirect_id` and `batch_id`. Returns the number of
        newly-inserted rows.
        """
        inserted = 0
        with Session(self.engine) as session:
            for item in rows:
                content_hash = item["content_hash"]
                comdirect_id = item.get("comdirect_id")

                existing = session.get(RawTransaction, content_hash)
                if existing is not None:
                    continue

                version = 1
                prev_hash: str | None = None
                if comdirect_id:
                    active = (
                        session.execute(
                            select(RawTransaction)
                            .where(RawTransaction.comdirect_id == comdirect_id)
                            .where(RawTransaction.superseded_by.is_(None))
                        )
                        .scalars()
                        .all()
                    )
                    if active:
                        prev = max(active, key=lambda r: r.version)
                        version = prev.version + 1
                        prev_hash = prev.content_hash

                session.add(
                    RawTransaction(
                        content_hash=content_hash,
                        comdirect_id=comdirect_id,
                        raw_data=item["raw_data"],
                        version=version,
                        batch_id=item.get("batch_id"),
                    )
                )
                if prev_hash is not None:
                    # FK superseded_by → content_hash requires the new row to
                    # exist before the old row points at it.
                    session.flush()
                    session.execute(
                        update(RawTransaction)
                        .where(RawTransaction.content_hash == prev_hash)
                        .values(superseded_by=content_hash)
                    )
                inserted += 1
            session.commit()
        return inserted

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def process_and_normalize(self) -> tuple[pd.DataFrame, str]:
        run_id = str(uuid.uuid4())
        with Session(self.engine) as session:
            session.add(
                SyncRun(
                    id=run_id, source=SyncSource.NORMALIZE, status=SyncStatus.RUNNING
                )
            )
            session.commit()

            try:
                df = self._build_dataframe(session)
                if df.empty:
                    self._finish_run(session, run_id, rows=0)
                    return df, run_id

                df = self._apply_rules(df, session)
                df = self._flag_internal_transfers(df, self.own_ibans)
                df, patterns = self._flag_recurring(df)
                df = self._flag_outliers(df)

                self._upsert_recurring_patterns(session, patterns, df)
                self._upsert_normalized(session, df)
                self._finish_run(session, run_id, rows=len(df))
                return df, run_id
            except Exception as exc:  # surface the failure in sync_runs
                self._finish_run(session, run_id, rows=0, error=str(exc))
                raise

    # ------------------------------------------------------------------
    # Helpers (pure where possible)
    # ------------------------------------------------------------------

    def _build_dataframe(self, session: Session) -> pd.DataFrame:
        active_rows = (
            session.execute(
                select(RawTransaction).where(RawTransaction.superseded_by.is_(None))
            )
            .scalars()
            .all()
        )

        records: list[dict[str, Any]] = []
        for raw in active_rows:
            canonical = canonicalize(raw.raw_data)
            if not canonical.get("booking_date"):
                continue
            records.append(
                {
                    "id": raw.content_hash,
                    "raw_content_hash": raw.content_hash,
                    "comdirect_id": canonical["comdirect_id"] or raw.comdirect_id,
                    "booking_date": canonical["booking_date"],
                    "valuation_date": canonical["valuation_date"]
                    or canonical["booking_date"],
                    "amount": canonical["amount"],
                    "currency": canonical["currency"],
                    "sender": canonical["sender"],
                    "recipient": canonical["recipient"],
                    "sender_iban": canonical["sender_iban"],
                    "recipient_iban": canonical["recipient_iban"],
                    "description": canonical["description"],
                    "category_id": None,
                    "is_recurring": False,
                    "is_outlier": False,
                    "internal_transfer": False,
                    "recurring_pattern_id": None,
                }
            )
        return pd.DataFrame(records)

    def _apply_rules(self, df: pd.DataFrame, session: Session) -> pd.DataFrame:
        rules = session.execute(select(Rule)).scalars().all()
        if not rules or df.empty:
            return df
        rules_sorted = sorted(rules, key=lambda r: r.priority, reverse=True)
        for idx, row in df.iterrows():
            text = " ".join(
                str(row.get(f) or "") for f in ("sender", "recipient", "description")
            ).lower()
            for rule in rules_sorted:
                if re.search(rule.regex_pattern, text, re.IGNORECASE):
                    df.at[idx, "category_id"] = rule.target_category_id
                    break
        return df

    @staticmethod
    def _flag_internal_transfers(
        df: pd.DataFrame, own_ibans: Iterable[str] | None = None
    ) -> pd.DataFrame:
        """Flag paired transfers between the user's own accounts.

        Bucketed by absolute cents to avoid N² comparison, paired on
        opposing signs within ±INTERNAL_TRANSFER_DAY_WINDOW days. When
        `own_ibans` is provided, both sides must reference IBANs from
        the set — this prevents false positives on coincidental ±X
        same-day pairs with external counterparties.
        """
        if df.empty:
            df["internal_transfer"] = False
            return df

        df = df.copy()
        df["internal_transfer"] = False
        df["_cents"] = df["amount"].apply(lambda a: int(Decimal(str(a)) * 100))
        df["_abs_cents"] = df["_cents"].abs()
        df["_bdate"] = pd.to_datetime(df["booking_date"])

        own = {i for i in (own_ibans or []) if i}

        for abs_cents, bucket in df.groupby("_abs_cents"):
            if abs_cents == 0 or len(bucket) < 2:
                continue
            positives = bucket[bucket["_cents"] > 0].to_dict("records")
            negatives = bucket[bucket["_cents"] < 0].to_dict("records")
            used: set[str] = set()
            for pos in positives:
                if pos["id"] in used:
                    continue
                for neg in negatives:
                    if neg["id"] in used:
                        continue
                    if (
                        abs((pos["_bdate"] - neg["_bdate"]).days)
                        > INTERNAL_TRANSFER_DAY_WINDOW
                    ):
                        continue
                    if own and not _both_ibans_in(pos, neg, own):
                        continue
                    df.loc[df["id"] == pos["id"], "internal_transfer"] = True
                    df.loc[df["id"] == neg["id"], "internal_transfer"] = True
                    used.add(pos["id"])
                    used.add(neg["id"])
                    break

        return df.drop(columns=["_cents", "_abs_cents", "_bdate"])

    @staticmethod
    def _flag_recurring(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Flag transactions that are part of a recurring series.

        Decision (2026-04-12): a recipient qualifies when it appears in
        at least RECURRING_MIN_MONTHS *consecutive* months with a
        standard deviation ≤ RECURRING_AMOUNT_TOLERANCE × mean(|amount|).
        Flagging is scoped to the consecutive run — isolated outliers
        from the same recipient do not get flagged.
        """
        if df.empty:
            df["is_recurring"] = False
            return df, []

        df = df.copy()
        df["is_recurring"] = False
        df["_bdate"] = pd.to_datetime(df["booking_date"])
        df["_month"] = df["_bdate"].dt.to_period("M")
        # Group by the counterpart: recipient for outgoing, sender for incoming.
        df["_counterpart"] = df["recipient"].where(
            df["recipient"].notna() & (df["recipient"] != ""), df["sender"]
        )

        patterns: list[dict[str, Any]] = []

        for recipient, group in df.groupby("_counterpart"):
            if not recipient:
                continue
            months_sorted = sorted(group["_month"].unique())
            runs = _consecutive_runs(months_sorted)
            for run in runs:
                if len(run) < RECURRING_MIN_MONTHS:
                    continue
                in_run = group[group["_month"].isin(run)]
                amounts = in_run["amount"].apply(lambda a: Decimal(str(a))).abs()
                mean = sum(amounts) / Decimal(len(amounts))
                if mean == 0:
                    continue
                variance = sum((a - mean) ** 2 for a in amounts) / Decimal(len(amounts))
                stddev = (
                    variance.sqrt()
                    if hasattr(variance, "sqrt")
                    else Decimal(str(float(variance) ** 0.5))
                )
                if stddev > mean * RECURRING_AMOUNT_TOLERANCE:
                    continue

                df.loc[in_run.index, "is_recurring"] = True
                patterns.append(
                    {
                        "recipient": recipient,
                        "avg_amount": mean.quantize(Decimal("0.01")),
                        "amount_stddev": stddev.quantize(Decimal("0.01")),
                        "first_seen_month": run[0].to_timestamp().date(),
                        "last_seen_month": run[-1].to_timestamp().date(),
                        "occurrence_count": int(len(in_run)),
                        "transaction_ids": list(in_run["id"]),
                    }
                )

        return df.drop(columns=["_bdate", "_month", "_counterpart"]), patterns

    @staticmethod
    def _flag_outliers(df: pd.DataFrame) -> pd.DataFrame:
        """Flag category-local outliers using the modified z-score (MAD).

        Mean/stddev-based z-scores suffer from masking: a single extreme
        value inflates both statistics and pulls its own z-score below
        the threshold, especially at small n. Median + MAD is robust
        because the median barely moves and the MAD ignores the outlier
        entirely.

        When the MAD collapses to zero (more than half the values are
        identical), fall back to the mean absolute deviation scaled to
        be a consistent estimator of stddev under normality.

        Internal transfers are excluded from detection — moving money
        between own accounts is not anomalous spending, and their
        amounts would otherwise distort the stats for the uncategorized
        bucket they often land in.
        """
        if df.empty:
            df["is_outlier"] = False
            return df
        df = df.copy()
        df["is_outlier"] = False
        amount_float = df["amount"].apply(lambda a: float(Decimal(str(a))))

        candidates = df[~df["internal_transfer"]]
        for _cat_id, group in candidates.groupby("category_id", dropna=False):
            if len(group) < 3:
                continue
            local = amount_float.loc[group.index]
            median = local.median()
            deviations = (local - median).abs()
            mad = deviations.median()
            if mad and not pd.isna(mad):
                # 0.6745 makes MAD a consistent estimator of σ for normal data.
                modified_z = 0.6745 * (local - median) / mad
            else:
                mean_ad = deviations.mean()
                if not mean_ad or pd.isna(mean_ad):
                    continue
                modified_z = (local - median) / (1.253314 * mean_ad)
            df.loc[
                group.index[modified_z.abs() > OUTLIER_MODIFIED_Z_THRESHOLD],
                "is_outlier",
            ] = True
        return df

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _upsert_recurring_patterns(
        self,
        session: Session,
        patterns: list[dict[str, Any]],
        df: pd.DataFrame,
    ) -> None:
        for pattern in patterns:
            record = RecurringPattern(
                recipient=pattern["recipient"],
                avg_amount=pattern["avg_amount"],
                amount_stddev=pattern["amount_stddev"],
                first_seen_month=pattern["first_seen_month"],
                last_seen_month=pattern["last_seen_month"],
                occurrence_count=pattern["occurrence_count"],
            )
            session.add(record)
            session.flush()
            df.loc[
                df["id"].isin(pattern["transaction_ids"]), "recurring_pattern_id"
            ] = record.id

    def _upsert_normalized(self, session: Session, df: pd.DataFrame) -> None:
        for _, row in df.iterrows():
            stmt = pg_insert(NormalizedTransaction).values(
                id=row["id"],
                raw_content_hash=row["raw_content_hash"],
                comdirect_id=_nan_to_none(row.get("comdirect_id")),
                booking_date=row["booking_date"],
                valuation_date=row["valuation_date"],
                amount=row["amount"],
                currency=_nan_to_none(row.get("currency")) or "EUR",
                sender=_nan_to_none(row.get("sender")),
                recipient=_nan_to_none(row.get("recipient")),
                sender_iban=_nan_to_none(row.get("sender_iban")),
                recipient_iban=_nan_to_none(row.get("recipient_iban")),
                description=_nan_to_none(row.get("description")),
                category_id=_nan_to_none(row.get("category_id")),
                is_recurring=bool(row["is_recurring"]),
                is_outlier=bool(row["is_outlier"]),
                internal_transfer=bool(row["internal_transfer"]),
                recurring_pattern_id=row.get("recurring_pattern_id"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "raw_content_hash": stmt.excluded.raw_content_hash,
                    "amount": stmt.excluded.amount,
                    "category_id": stmt.excluded.category_id,
                    "is_recurring": stmt.excluded.is_recurring,
                    "is_outlier": stmt.excluded.is_outlier,
                    "internal_transfer": stmt.excluded.internal_transfer,
                    "recurring_pattern_id": stmt.excluded.recurring_pattern_id,
                    "description": stmt.excluded.description,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            session.execute(stmt)
        session.commit()

    def _finish_run(
        self, session: Session, run_id: str, *, rows: int, error: str | None = None
    ) -> None:
        session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                status=SyncStatus.FAILED if error else SyncStatus.SUCCEEDED,
                finished_at=datetime.now(timezone.utc),
                rows_processed=rows,
                error=error,
            )
        )
        session.commit()


def _both_ibans_in(a: dict[str, Any], b: dict[str, Any], own: set[str]) -> bool:
    a_ibans = {a.get("sender_iban"), a.get("recipient_iban")}
    b_ibans = {b.get("sender_iban"), b.get("recipient_iban")}
    return bool((a_ibans & own) and (b_ibans & own))


def _consecutive_runs(months: list[pd.Period]) -> list[list[pd.Period]]:
    """Split a sorted list of pandas Periods into runs of consecutive months."""
    if not months:
        return []
    runs: list[list[pd.Period]] = [[months[0]]]
    for m in months[1:]:
        if (m.ordinal - runs[-1][-1].ordinal) == 1:
            runs[-1].append(m)
        else:
            runs.append([m])
    return runs


def _nan_to_none(value: Any) -> Any:
    """Convert pandas NaN / NaT to Python None for DB insertion."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value
