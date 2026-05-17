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
from typing import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.core.db.models import (
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    RecurringPattern,
    Rule,
    SyncRun,
    SyncStage,
    SyncStatus,
)
from src.normalization.canonicalize import canonicalize
from src.normalization.paypal_csv import paypal_csv_canonicalize
from src.normalization.santander_canonicalize import santander_canonicalize

logger = logging.getLogger(__name__)

INTERNAL_TRANSFER_DAY_WINDOW = 2
# Tolerance on the Santander credit-card billing-period boundary when
# matching the Comdirect settlement lump posting (M16-P2c).
SANTANDER_SETTLEMENT_DAY_WINDOW = 3

# Source-aware canonicalisation. `_build_dataframe` re-projects every active
# raw payload onto the canonical shape; a PayPal or Santander payload is
# nothing like the Comdirect shape, so the generic `canonicalize()` would
# silently drop it (no booking_date → skipped). Dispatch on the row's
# `source` so each provider's own adapter runs.
_CANONICALIZERS = {
    DataSource.COMDIRECT: canonicalize,
    DataSource.PAYPAL: paypal_csv_canonicalize,
    DataSource.SANTANDER_CC: santander_canonicalize,
}


def _canonicalize_for_source(source: Any, raw_data: dict[str, Any]) -> dict[str, Any]:
    """Project a raw payload onto the canonical dict using the source's adapter."""
    if not isinstance(source, DataSource):
        try:
            source = DataSource(source)
        except ValueError:
            source = DataSource.COMDIRECT
    return _CANONICALIZERS.get(source, canonicalize)(raw_data)
RECURRING_MIN_MONTHS = 3
RECURRING_AMOUNT_TOLERANCE = Decimal("0.10")  # ±10 %
# Modified z-score threshold (Iglewicz & Hoaglin, 1993). Robust against
# masking by extreme values — a property mean/stddev-based z-scores lack
# at small sample sizes.
OUTLIER_MODIFIED_Z_THRESHOLD = 3.5


# ---------------------------------------------------------------------------
# Rule matching — shared between the normalization pipeline and the
# `POST /categories/rules/apply-all` endpoint so backend behaviour is
# guaranteed identical across both call sites. Keep the haystack/regex
# semantics in lockstep with the UI's RegexPreview component.
# ---------------------------------------------------------------------------


def build_rule_haystack(
    *,
    sender: str | None,
    recipient: str | None,
    description: str | None,
) -> str:
    """Build the case-folded haystack used for rule regex matching.

    Mirrors the UI's `buildHaystack` (k-fin-ui RulesSection.tsx): join
    sender, recipient, description with single spaces, lowercase the
    result. The space-joined order is part of the contract — patterns
    like `^rewe` rely on it. Empty/None fields render as ``""``.
    """
    parts = [str(sender or ""), str(recipient or ""), str(description or "")]
    return " ".join(parts).lower()


def sort_rules_by_priority(rules: Sequence[Rule]) -> list[Rule]:
    """Sort rules so the highest priority (largest int) wins ties first.

    Stable on equal priority so DB insertion order acts as the
    secondary tie-breaker — predictable for users editing rules in the
    UI in the order they want them tried.
    """
    return sorted(rules, key=lambda r: r.priority, reverse=True)


def match_rule(rules_sorted: Sequence[Rule], haystack: str) -> Rule | None:
    """Return the first rule whose `regex_pattern` matches the haystack.

    Case-insensitive `re.search` (not `match`) — the rule fires when the
    pattern appears anywhere in the joined sender/recipient/description.
    Invalid regex from the DB is treated as a non-match so a single bad
    rule never poisons the whole apply pass; the API rejects invalid
    patterns at write time so this is a defensive fallback.
    """
    for rule in rules_sorted:
        try:
            if re.search(rule.regex_pattern, haystack, re.IGNORECASE):
                return rule
        except re.error:
            logger.warning(
                "rule %s has invalid regex %r; skipped during apply",
                rule.id,
                rule.regex_pattern,
            )
            continue
    return None


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
        optionally `source`, `external_id`, and `batch_id`. Returns the
        number of newly-inserted rows. Rows without an explicit `source`
        default to `DataSource.COMDIRECT` for backward compatibility.
        """
        inserted = 0
        with Session(self.engine) as session:
            for item in rows:
                content_hash = item["content_hash"]
                external_id = item.get("external_id")
                source = item.get("source") or DataSource.COMDIRECT

                existing = session.get(RawTransaction, content_hash)
                if existing is not None:
                    continue

                version = 1
                prev_hash: str | None = None
                if external_id:
                    active = (
                        session.execute(
                            select(RawTransaction)
                            .where(RawTransaction.source == source)
                            .where(RawTransaction.external_id == external_id)
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
                        source=source,
                        external_id=external_id,
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
                    id=run_id, source=SyncStage.NORMALIZE, status=SyncStatus.RUNNING
                )
            )
            session.commit()

            try:
                df = self._build_dataframe(session)
                if df.empty:
                    self._finish_run(session, run_id, rows=0)
                    return df, run_id

                df = self._enrich_paypal_purchases(df)
                df = self._apply_rules(df, session)
                df = self._flag_internal_transfers(df, self.own_ibans)
                df = self._flag_cross_source_transfers(df)
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
            canonical = _canonicalize_for_source(raw.source, raw.raw_data)
            if not canonical.get("booking_date"):
                continue
            records.append(
                {
                    "id": raw.content_hash,
                    "raw_content_hash": raw.content_hash,
                    "source": raw.source,
                    "external_id": canonical["external_id"] or raw.external_id,
                    "booking_date": canonical["booking_date"],
                    "valuation_date": canonical["valuation_date"]
                    or canonical["booking_date"],
                    "amount": canonical["amount"],
                    "currency": canonical["currency"],
                    # FX leg (M16-P2c) — present only for non-EUR Santander
                    # credit-card purchases; None everywhere else.
                    "original_amount": canonical.get("original_amount"),
                    "original_currency": canonical.get("original_currency"),
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

    @staticmethod
    def _enrich_paypal_purchases(df: pd.DataFrame) -> pd.DataFrame:
        """Copy PayPal merchant context onto the matching Comdirect lines.

        A PayPal-funded purchase appears twice: the bank statement shows
        only an anonymous "PAYPAL (EUROPE) S.A.R.L." direct debit, while
        the PayPal row carries the real merchant and item. This pass joins
        the two (equal amount, ±INTERNAL_TRANSFER_DAY_WINDOW days) and
        rewrites the bank row's ``recipient`` / ``description`` with the
        merchant. It runs **before `_apply_rules`** so rules and the
        categoriser match on the real merchant, not on "PAYPAL EUROPE".

        The matched PayPal row is the duplicate leg of that spend; it is
        marked in the temp column ``_pp_purchase_dup``, which
        `_flag_cross_source_transfers` turns into ``internal_transfer`` so
        the spend is counted exactly once.
        """
        if df.empty:
            return df
        df = df.copy()
        df["_pp_purchase_dup"] = False

        matches = _match_paypal_purchases(df.to_dict("records"))
        for match in matches:
            merchant = match["merchant"]
            if merchant:
                bank_row = df["id"] == match["comdirect_id"]
                existing = df.loc[bank_row, "description"]
                bank_desc = _nan_to_none(existing.iloc[0]) if len(existing) else None
                df.loc[bank_row, "recipient"] = merchant
                df.loc[bank_row, "description"] = _merge_paypal_context(
                    merchant, bank_desc
                )
            df.loc[df["id"] == match["paypal_id"], "_pp_purchase_dup"] = True

        if matches:
            logger.info(
                "PayPal: enriched %d Comdirect 'PAYPAL' line(s) with merchant context",
                len(matches),
            )
        return df

    def _apply_rules(self, df: pd.DataFrame, session: Session) -> pd.DataFrame:
        rules = session.execute(select(Rule)).scalars().all()
        if not rules or df.empty:
            return df
        rules_sorted = sort_rules_by_priority(rules)
        for idx, row in df.iterrows():
            text = build_rule_haystack(
                sender=row.get("sender"),
                recipient=row.get("recipient"),
                description=row.get("description"),
            )
            match = match_rule(rules_sorted, text)
            if match is not None:
                df.at[idx, "category_id"] = match.target_category_id
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

        Rows without any IBAN are never paired here: PayPal and
        Santander credit-card rows carry no IBAN and cannot be legs of
        an own-account bank transfer. Pairing them mis-flags a PayPal
        payment and its same-amount refund as an internal transfer.
        Cross-source matching for those sources lives in
        `_flag_cross_source_transfers`.
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
                    # A row with no IBAN at all cannot be a leg of an
                    # own-account bank transfer — guard before the
                    # `own_ibans` check so it holds even when no own
                    # IBANs are configured. Without it, IBAN-less PayPal
                    # rows (a payment and its refund) get mis-paired.
                    if not (_has_iban(pos) and _has_iban(neg)):
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
    def _flag_cross_source_transfers(df: pd.DataFrame) -> pd.DataFrame:
        """Cross-source internal-transfer matching (M16-P2b / P2c).

        The IBAN matcher above only pairs Comdirect↔Comdirect transfers
        (PayPal/Santander carry no IBAN). This site matches the collapsed
        counterparts on amount + date instead, in two independent passes:

        - **P2b — PayPal top-up.** A PayPal "Bank Deposit to PP Account"
          credit against the Comdirect "PAYPAL EUROPE" debit lump posting,
          opposite signs, exact amount, ±INTERNAL_TRANSFER_DAY_WINDOW days
          → **both** `internal_transfer=True`.

        - **P2c — Santander credit-card settlement.** The Comdirect
          "Santander … Kartenabrechnung" debit lump posting against the
          *sum* of the Santander credit-card transactions in that billing
          cycle, exact sum match → **only the Comdirect lump posting**
          `internal_transfer=True`. The individual Santander charges stay
          un-flagged: they are the real spend and must be counted exactly
          once (in the Santander source, with the real merchant). A
          partial payment / instalment (Teilzahlung) makes the debit ≠ the
          cycle sum, so it simply does not match — no false positive.

        It also turns the ``_pp_purchase_dup`` marker — set upstream by
        `_enrich_paypal_purchases` on a PayPal spend whose merchant was
        copied onto a Comdirect "PAYPAL EUROPE" line — into
        ``internal_transfer``, so that spend is counted exactly once.

        Each side is consumed at most once per match so a run of equal-
        value transfers cannot fan out into spurious pairings.
        """
        if df.empty:
            return df

        df = df.copy()
        records = df.to_dict("records")

        flagged: set[Any] = set()
        flagged |= _match_paypal_topups(records)
        flagged |= _match_santander_cc_settlement(records)

        # A PayPal spend whose merchant was copied onto a Comdirect
        # "PAYPAL EUROPE" line (see `_enrich_paypal_purchases`) is the
        # duplicate leg of that spend — flag it so the amount counts once.
        if "_pp_purchase_dup" in df.columns:
            flagged |= set(df.loc[df["_pp_purchase_dup"].astype(bool), "id"])
            df = df.drop(columns=["_pp_purchase_dup"])

        if flagged:
            df.loc[df["id"].isin(flagged), "internal_transfer"] = True
        return df

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
                source=row.get("source") or DataSource.COMDIRECT,
                external_id=_nan_to_none(row.get("external_id")),
                booking_date=row["booking_date"],
                valuation_date=row["valuation_date"],
                amount=row["amount"],
                currency=_nan_to_none(row.get("currency")) or "EUR",
                original_amount=_nan_to_none(row.get("original_amount")),
                original_currency=_nan_to_none(row.get("original_currency")),
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
            # Preserve any category_id already set on the existing row
            # (by the categorization agent, a manual edit, or a prior rule
            # match). Without COALESCE, every re-run of process_and_normalize
            # would overwrite agent/user categories with NULL or a stale
            # rule result — see the post-mortem of the prod incident on
            # 2026-05-01 where a sync wiped all categorized transactions.
            # If a user wants to (re-)apply rules to already-categorized
            # rows, that's an explicit separate action — not a side effect
            # of normalisation.
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "raw_content_hash": stmt.excluded.raw_content_hash,
                    "amount": stmt.excluded.amount,
                    "category_id": func.coalesce(
                        NormalizedTransaction.category_id,
                        stmt.excluded.category_id,
                    ),
                    "is_recurring": stmt.excluded.is_recurring,
                    "is_outlier": stmt.excluded.is_outlier,
                    "internal_transfer": stmt.excluded.internal_transfer,
                    "recurring_pattern_id": stmt.excluded.recurring_pattern_id,
                    "description": stmt.excluded.description,
                    "original_amount": stmt.excluded.original_amount,
                    "original_currency": stmt.excluded.original_currency,
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


def _has_iban(rec: dict[str, Any]) -> bool:
    """Whether a row carries at least one IBAN — i.e. it *could* be a leg
    of an own-account bank transfer. PayPal and Santander credit-card
    rows carry neither a sender nor a recipient IBAN; a pandas NaN cell
    is treated as absent (guard on the type — NaN is truthy)."""
    for key in ("sender_iban", "recipient_iban"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _source_value(source: Any) -> str:
    """Normalise a DataFrame `source` cell — DataSource enum or str — to its value."""
    return source.value if isinstance(source, DataSource) else str(source)


def _is_paypal_bank_deposit(rec: dict[str, Any]) -> bool:
    """A PayPal credit funding the balance from a bank account.

    The canonical adapter (`paypal_canonicalize`) stamps these with the
    stable "Bank Deposit to PP Account" description; the deposit is a
    credit (amount > 0 — money arriving in PayPal).
    """
    if _source_value(rec.get("source")) != DataSource.PAYPAL.value:
        return False
    if not rec.get("amount") or rec["amount"] <= 0:
        return False
    # `description` may arrive as a pandas NaN (float) for a row that had
    # none — NaN is truthy, so guard on the type, not on `or ""`.
    desc = rec.get("description")
    return isinstance(desc, str) and "bank deposit" in desc.lower()


def _is_comdirect_paypal_posting(rec: dict[str, Any]) -> bool:
    """The Comdirect "PAYPAL EUROPE" debit — the bank side of a top-up.

    A debit (amount < 0 — money leaving the bank account) whose
    counterparty or remittance text names PayPal.
    """
    if _source_value(rec.get("source")) != DataSource.COMDIRECT.value:
        return False
    if not rec.get("amount") or rec["amount"] >= 0:
        return False
    haystack = " ".join(
        str(part) for part in (rec.get("description"), rec.get("recipient")) if part
    ).lower()
    return "paypal" in haystack


def _is_comdirect_santander_posting(rec: dict[str, Any]) -> bool:
    """The Comdirect "Santander … Kartenabrechnung" debit — the bank side
    of a credit-card settlement.

    A debit (amount < 0 — money leaving the bank account) whose counterparty
    or remittance text names Santander.
    """
    if _source_value(rec.get("source")) != DataSource.COMDIRECT.value:
        return False
    if not rec.get("amount") or rec["amount"] >= 0:
        return False
    haystack = " ".join(
        str(part) for part in (rec.get("description"), rec.get("recipient")) if part
    ).lower()
    return "santander" in haystack


def _match_paypal_topups(records: list[dict[str, Any]]) -> set[Any]:
    """Pair PayPal bank-deposit credits with their Comdirect "PAYPAL" debits.

    Returns the ids of every matched row (both sides). Each Comdirect
    posting is consumed at most once.
    """
    pp_deposits = [r for r in records if _is_paypal_bank_deposit(r)]
    cd_paypal = [r for r in records if _is_comdirect_paypal_posting(r)]
    if not pp_deposits or not cd_paypal:
        return set()

    flagged: set[Any] = set()
    used_comdirect: set[Any] = set()
    for deposit in pp_deposits:
        dep_cents = abs(int(Decimal(str(deposit["amount"])) * 100))
        dep_date = pd.to_datetime(deposit["booking_date"])
        for posting in cd_paypal:
            if posting["id"] in used_comdirect:
                continue
            post_cents = abs(int(Decimal(str(posting["amount"])) * 100))
            if post_cents != dep_cents:
                continue
            if (
                abs((dep_date - pd.to_datetime(posting["booking_date"])).days)
                > INTERNAL_TRANSFER_DAY_WINDOW
            ):
                continue
            flagged.add(deposit["id"])
            flagged.add(posting["id"])
            used_comdirect.add(posting["id"])
            break
    return flagged


def _match_santander_cc_settlement(records: list[dict[str, Any]]) -> set[Any]:
    """Match a Comdirect credit-card settlement debit to a Santander billing cycle.

    The billing cycle is taken as the calendar month of the Santander
    credit-card transactions; its expected settlement is the exact *net*
    sum of that month's charges (purchases minus refunds). A Comdirect
    "Santander" debit whose amount equals that sum is the lump posting —
    only it is returned for flagging (the individual Santander charges are
    the real spend and stay un-flagged). Each cycle is consumed once.
    """
    santander = [
        r
        for r in records
        if _source_value(r.get("source")) == DataSource.SANTANDER_CC.value
    ]
    cd_postings = [r for r in records if _is_comdirect_santander_posting(r)]
    if not santander or not cd_postings:
        return set()

    # Bucket the Santander charges by calendar month → net sum + date span.
    buckets: dict[tuple[int, int], dict[str, Any]] = {}
    for rec in santander:
        bdate = pd.to_datetime(rec["booking_date"])
        key = (bdate.year, bdate.month)
        bucket = buckets.setdefault(
            key, {"sum_cents": 0, "min_date": bdate, "max_date": bdate}
        )
        bucket["sum_cents"] += int(Decimal(str(rec["amount"])) * 100)
        bucket["min_date"] = min(bucket["min_date"], bdate)
        bucket["max_date"] = max(bucket["max_date"], bdate)

    tolerance = pd.Timedelta(days=SANTANDER_SETTLEMENT_DAY_WINDOW)
    flagged: set[Any] = set()
    used_cycles: set[tuple[int, int]] = set()
    for posting in sorted(cd_postings, key=lambda r: pd.to_datetime(r["booking_date"])):
        post_cents = abs(int(Decimal(str(posting["amount"])) * 100))
        post_date = pd.to_datetime(posting["booking_date"])
        for key in sorted(buckets):
            if key in used_cycles:
                continue
            bucket = buckets[key]
            if abs(bucket["sum_cents"]) != post_cents:
                continue
            # The settlement debit posts no earlier than the billing cycle
            # opened (minus the tolerance) — guards against pairing a debit
            # with a far-off cycle that happens to net the same sum.
            if post_date < bucket["min_date"] - tolerance:
                continue
            flagged.add(posting["id"])
            used_cycles.add(key)
            break
    return flagged


def _is_paypal_outgoing(rec: dict[str, Any]) -> bool:
    """A PayPal debit that is a real spend — a purchase or a sent payment.

    Excludes the bank-withdrawal event (money moved PayPal → bank): that
    is also a debit, but an internal transfer, not a spend.
    """
    if _source_value(rec.get("source")) != DataSource.PAYPAL.value:
        return False
    if not rec.get("amount") or rec["amount"] >= 0:
        return False
    # A pandas NaN `description` (float) is not a withdrawal label — guard
    # on the type rather than on `or ""`, since NaN is truthy.
    desc = rec.get("description")
    return not (isinstance(desc, str) and "withdrawal to bank" in desc.lower())


def _match_paypal_purchases(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each PayPal spend with its anonymous Comdirect "PAYPAL" debit.

    Paired on equal absolute amount within ±INTERNAL_TRANSFER_DAY_WINDOW
    days; each Comdirect posting is consumed at most once. Ambiguity is
    resolved by *not guessing* — if a posting could match more than one
    PayPal spend, it is skipped (a missing enrichment beats a wrong one).

    Returns one dict per confident match, the PayPal merchant already
    NaN-cleaned: ``{comdirect_id, paypal_id, merchant}``.
    """
    purchases = [r for r in records if _is_paypal_outgoing(r)]
    cd_postings = [r for r in records if _is_comdirect_paypal_posting(r)]
    if not purchases or not cd_postings:
        return []

    matches: list[dict[str, Any]] = []
    used_paypal: set[Any] = set()
    for posting in cd_postings:
        post_cents = abs(int(Decimal(str(posting["amount"])) * 100))
        post_date = pd.to_datetime(posting["booking_date"])
        candidates = [
            p
            for p in purchases
            if p["id"] not in used_paypal
            and abs(int(Decimal(str(p["amount"])) * 100)) == post_cents
            and abs((post_date - pd.to_datetime(p["booking_date"])).days)
            <= INTERNAL_TRANSFER_DAY_WINDOW
        ]
        if len(candidates) != 1:
            continue  # 0 → nothing to pair; >1 → ambiguous, do not guess
        purchase = candidates[0]
        used_paypal.add(purchase["id"])
        matches.append(
            {
                "comdirect_id": posting["id"],
                "paypal_id": purchase["id"],
                "merchant": _nan_to_none(purchase.get("recipient")),
            }
        )
    return matches


def _merge_paypal_context(merchant: str, bank_desc: str | None) -> str:
    """Compose the enriched bank-line description: the PayPal merchant
    first (so rule matching and the categoriser see it), the original
    bank text kept after a ``·`` separator so the line still reads as a
    PayPal posting."""
    bank = (bank_desc or "").strip()
    return f"{merchant} · {bank}"[:500] if bank else merchant[:500]


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
