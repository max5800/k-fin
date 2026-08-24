"""Normalization pipeline: raw payloads → tagged, deduped transactions.

Idempotency contract:
- Identical raw payloads produce identical content hashes → skipped on re-ingest.
- Comdirect-corrected payloads produce a new hash → inserted as version=old+1,
  old row's `superseded_by` is pointed at the new hash. No data is lost.

Only raw rows with `superseded_by IS NULL` flow into normalized_transactions.
"""

from __future__ import annotations

import calendar
import logging
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

import pandas as pd
from sqlalchemy import case, create_engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.core.db.models import (
    AppSettings,
    Category,
    DataSource,
    NormalizedTransaction,
    RawTransaction,
    RecurringPattern,
    Rule,
    SourceStatementPeriod,
    SyncRun,
    SyncStage,
    SyncStatus,
    TransactionLink,
    TypeEnum,
)
from src.normalization.canonicalize import canonicalize
from src.normalization.paypal_csv import BANK_DEPOSIT_LABEL, paypal_csv_canonicalize
from src.normalization.santander_canonicalize import santander_canonicalize

logger = logging.getLogger(__name__)

INTERNAL_TRANSFER_DAY_WINDOW = 2
# Tolerance on the Santander credit-card billing-period boundary when
# matching the Comdirect settlement lump posting (M16-P2c).
SANTANDER_SETTLEMENT_DAY_WINDOW = 3
# A card settlement must arrive reasonably close to the final transaction in
# its billing cycle.  Forty-five days covers a month-end statement followed by
# a normal collection window while refusing equal-value postings from a much
# later cycle.
SANTANDER_SETTLEMENT_MAX_LAG_DAYS = 45
MAX_PAYPAL_AGGREGATE_CANDIDATES = 18
MAX_PAYPAL_EXACT_SUBSETS = 256
MAX_GLOBAL_MATCHING_STATES = 100_000

PAYPAL_AGGREGATE_LINK = "paypal_aggregate"
SANTANDER_SETTLEMENT_LINK = "santander_settlement"
PAYPAL_TOPUP_LINK = "paypal_topup"
AUTO_TRANSACTION_LINK_TYPES = (
    PAYPAL_TOPUP_LINK,
    PAYPAL_AGGREGATE_LINK,
    SANTANDER_SETTLEMENT_LINK,
)

ACCOUNTING_VERSION = 2
ASSET_CATEGORY_IDS = {"etf-sparplan", "wertpapierkauf"}
DEBT_CATEGORY_IDS = {"kredit-tilgung"}
FEE_INTEREST_CATEGORY_IDS = {"gebuehren-zinsen"}
TRANSFER_CATEGORY_IDS = {"umbuchung"}
SUBSCRIPTION_CATEGORY_IDS = {"abos-streaming", "mitgliedschaften", "oepnv-abo"}
FIXED_COST_CATEGORY_IDS = {
    "auto-fix",
    "gez",
    "internet-telefon",
    "miete",
    "strom-gas",
    "versicherungen",
}
# These are the exact labels emitted by the fail-closed Santander statement
# parser for rows that have no purchase date.  Exact matching prevents a
# merchant whose name merely contains e.g. "Zins" from being guessed into a
# financial charge partition.
FEE_INTEREST_LABELS = {
    "finanzierungsgebühr",
    "mahngebühr",
    "sollzinsen",
    "verzugszinsen",
}

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
    """Project a raw payload onto the canonical dict using the source's adapter.

    An unrecognised `source` is a loud failure, not a silent fallback:
    routing a non-Comdirect payload through the Comdirect `canonicalize()`
    would drop it (no `booking_date` → skipped in `_build_dataframe`) and
    lose financial data without a trace. The raised error surfaces on the
    `SyncRun` row instead.
    """
    if not isinstance(source, DataSource):
        try:
            source = DataSource(source)
        except ValueError as exc:
            raise ValueError(
                f"cannot canonicalize raw transaction: unknown source {source!r}"
            ) from exc
    try:
        return _CANONICALIZERS[source](raw_data)
    except KeyError as exc:
        raise ValueError(
            f"no canonical adapter registered for source {source.value!r}"
        ) from exc


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


def _load_own_ibans(engine) -> list[str]:
    """Load the user's own-account IBANs from the ``app_settings`` singleton.

    Defensive: a missing table / column (a fresh in-memory test DB) or any
    DB error yields an empty list rather than failing pipeline construction.
    """
    try:
        with Session(engine) as session:
            row = session.get(AppSettings, 1)
            ibans = row.own_ibans if row is not None else ""
    except SQLAlchemyError:
        return []
    return [s.strip() for s in (ibans or "").split(",") if s.strip()]


class NormalizationPipeline:
    def __init__(self, database_url: str, own_ibans: Iterable[str] | None = None):
        self.engine = create_engine(database_url)
        # `own_ibans` defaults to the UI-managed list on the app_settings
        # row; an explicit argument (tests, special callers) overrides it.
        if own_ibans is None:
            own_ibans = _load_own_ibans(self.engine)
        self.own_ibans = {i for i in (own_ibans or []) if i}

    # ------------------------------------------------------------------
    # Raw ingest with content-hash versioning
    # ------------------------------------------------------------------

    def load_raw_transactions(
        self,
        rows: list[dict[str, Any]],
        *,
        session: Session | None = None,
        commit: bool = True,
    ) -> int:
        """Insert raw rows with versioning.

        Each row dict must provide: `content_hash`, `raw_data`, and
        optionally `source`, `external_id`, and `batch_id`. Returns the
        number of newly-inserted rows. Rows without an explicit `source`
        default to `DataSource.COMDIRECT` for backward compatibility.
        """
        if session is None:
            with Session(self.engine) as owned_session:
                return self.load_raw_transactions(
                    rows, session=owned_session, commit=True
                )

        inserted = 0
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
                        .with_for_update()
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
        if commit:
            session.commit()
        return inserted

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def process_and_normalize(
        self,
        *,
        session: Session | None = None,
        commit: bool = True,
    ) -> tuple[pd.DataFrame, str]:
        if session is None:
            with Session(self.engine) as owned_session:
                return self.process_and_normalize(
                    session=owned_session, commit=True
                )

        run_id = str(uuid.uuid4())
        session.add(
            SyncRun(
                id=run_id, source=SyncStage.NORMALIZE, status=SyncStatus.RUNNING
            )
        )
        if commit:
            session.commit()
        else:
            session.flush()

        try:
            df = self._build_dataframe(session)
            if df.empty:
                # Empty canonical input is a successful zero-row pass, not a
                # parse failure.  Housekeeping still runs so PostgreSQL and the
                # public SyncRun contract both end at SUCCEEDED/0.
                self._supersede_stale_normalized(session, commit=commit)
                self._replace_transaction_links(session, [], commit=commit)
                self._refresh_accounting_classification(session, commit=commit)
                self._finish_run(session, run_id, rows=0, commit=commit)
                return df, run_id

            df = self._apply_rules(df, session)
            df = self._flag_internal_transfers(df, self.own_ibans)
            df, transaction_links = self._reconcile_cross_source_transfers(df)
            df, patterns = self._flag_recurring(df)
            df = self._flag_outliers(df)

            self._upsert_recurring_patterns(session, patterns, df)
            self._upsert_normalized(session, df, commit=commit)
            self._supersede_stale_normalized(session, commit=commit)
            self._replace_transaction_links(
                session, transaction_links, commit=commit
            )
            self._refresh_accounting_classification(session, commit=commit)
            self._record_source_periods(session, df, commit=commit)
            self._finish_run(session, run_id, rows=len(df), commit=commit)
            return df, run_id
        except Exception as exc:  # surface ordinary pipeline failures in sync_runs
            if commit:
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
                    "normalization_version": raw.version,
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
        """Compatibility wrapper for tests/callers that only need flags."""
        flagged, _links = NormalizationPipeline._reconcile_cross_source_transfers(df)
        return flagged

    @staticmethod
    def _reconcile_cross_source_transfers(
        df: pd.DataFrame,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Flag cross-source aggregate parents and build transaction links.

        The IBAN matcher covers own-account bank transfers. Imported PayPal and
        Santander rows need a separate, conservative reconciliation layer:

        - PayPal balance top-ups remain a 1:1 internal-transfer pair.
        - PayPal purchase aggregates are linked only when one anonymous
          PayPal posting equals exactly one unique net subset of PayPal detail
          rows within the date window.
        - Santander card settlements link the Comdirect settlement debit to
          every Santander row in the matching billing month when the exact net
          sum matches.

        Parent aggregate rows are marked ``internal_transfer=True`` so they do
        not inflate spend; detail rows keep their existing flags and remain the
        counted source of truth. Ambiguous or residual matches stay unlinked.
        """
        if df.empty:
            return df, []

        df = df.copy()
        records = df.to_dict("records")

        flagged: set[Any] = set()
        links: list[dict[str, Any]] = []

        topup_pairs = _match_paypal_topups(records)
        paypal_topup_parents = {parent_id for parent_id, _child_id in topup_pairs}
        for parent_id, child_id in topup_pairs:
            flagged.add(parent_id)
            flagged.add(child_id)
            links.append(
                _build_transaction_link(
                    parent_id=parent_id,
                    child_id=child_id,
                    link_type=PAYPAL_TOPUP_LINK,
                )
            )

        for match in _match_paypal_aggregate_sets(
            records, excluded_parent_ids=paypal_topup_parents
        ):
            parent_id = match["parent_id"]
            flagged.add(parent_id)
            for child_id in match["child_ids"]:
                links.append(
                    _build_transaction_link(
                        parent_id=parent_id,
                        child_id=child_id,
                        link_type=PAYPAL_AGGREGATE_LINK,
                    )
                )

        for match in _match_santander_cc_settlements(records):
            parent_id = match["parent_id"]
            flagged.add(parent_id)
            for child_id in match["child_ids"]:
                links.append(
                    _build_transaction_link(
                        parent_id=parent_id,
                        child_id=child_id,
                        link_type=SANTANDER_SETTLEMENT_LINK,
                    )
                )

        if flagged:
            df.loc[df["id"].isin(flagged), "internal_transfer"] = True
        return df, links

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

    def _upsert_normalized(
        self, session: Session, df: pd.DataFrame, *, commit: bool = True
    ) -> None:
        for _, row in df.iterrows():
            stmt = pg_insert(NormalizedTransaction).values(
                id=row["id"],
                raw_content_hash=row["raw_content_hash"],
                source=row.get("source") or DataSource.COMDIRECT,
                external_id=_nan_to_none(row.get("external_id")),
                normalization_version=int(row["normalization_version"]),
                normalization_status="active",
                is_active=True,
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
                    "normalization_status": "active",
                    "normalization_version": stmt.excluded.normalization_version,
                    "is_active": True,
                    "superseded_by_id": None,
                    "amount": stmt.excluded.amount,
                    "sender": stmt.excluded.sender,
                    "recipient": stmt.excluded.recipient,
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
        if commit:
            session.commit()

    @staticmethod
    def _supersede_stale_normalized(
        session: Session, *, commit: bool = True
    ) -> None:
        """Mirror immutable raw correction chains without deleting history."""
        stale = session.execute(
            select(NormalizedTransaction, RawTransaction.superseded_by)
            .join(RawTransaction, RawTransaction.content_hash == NormalizedTransaction.raw_content_hash)
            .where(RawTransaction.superseded_by.is_not(None))
            .where(NormalizedTransaction.is_active.is_(True))
        ).all()
        for tx, successor_id in stale:
            tx.is_active = False
            tx.normalization_status = "superseded"
            tx.superseded_by_id = successor_id

        # Every link type participates in the same active/history contract.
        # Retain links involving a superseded row, but never leave them active
        # where API traversal could reach an inactive participant.
        stale_ids = [tx.id for tx, _successor_id in stale]
        if stale_ids:
            session.execute(
                update(TransactionLink)
                .where(TransactionLink.is_active.is_(True))
                .where(
                    TransactionLink.parent_transaction_id.in_(stale_ids)
                    | TransactionLink.child_transaction_id.in_(stale_ids)
                )
                .values(
                    is_active=False,
                    status="invalid_participant",
                    version=TransactionLink.version + 1,
                )
            )

        # A previously superseded row can be restored safely if an operator
        # reverses the raw-chain pointer; rerunning normalization is enough.
        session.execute(
            update(NormalizedTransaction)
            .where(
                NormalizedTransaction.raw_content_hash.in_(
                    select(RawTransaction.content_hash).where(
                        RawTransaction.superseded_by.is_(None)
                    )
                )
            )
            .values(is_active=True, normalization_status="active", superseded_by_id=None)
        )
        if commit:
            session.commit()

    @staticmethod
    def _replace_transaction_links(
        session: Session,
        links: list[dict[str, Any]],
        *,
        commit: bool = True,
    ) -> None:
        """Version auto-derived links idempotently; never delete audit rows."""
        seen: set[tuple[str, str, str]] = set()
        unique_links: list[dict[str, Any]] = []
        for link in links:
            key = (
                str(link["parent_transaction_id"]),
                str(link["child_transaction_id"]),
                str(link["link_type"]),
            )
            if key in seen:
                continue
            seen.add(key)
            unique_links.append(link)

        stale_stmt = (
            update(TransactionLink)
            .where(TransactionLink.link_type.in_(AUTO_TRANSACTION_LINK_TYPES))
            .where(TransactionLink.is_active.is_(True))
        )
        derived_ids = [str(link["id"]) for link in unique_links]
        if derived_ids:
            stale_stmt = stale_stmt.where(TransactionLink.id.not_in(derived_ids))
        session.execute(
            stale_stmt.values(
                is_active=False,
                status="superseded",
                version=TransactionLink.version + 1,
            )
        )

        for link in unique_links:
            stmt = pg_insert(TransactionLink).values(
                **link,
                status="active",
                is_active=True,
                version=1,
                confidence=Decimal("1.000"),
                match_reason="unique exact amount/date reconciliation",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "status": "active",
                    "is_active": True,
                    "version": TransactionLink.version
                    + case((TransactionLink.is_active.is_(False), 1), else_=0),
                    "confidence": stmt.excluded.confidence,
                    "match_reason": stmt.excluded.match_reason,
                },
            )
            session.execute(stmt)
        if commit:
            session.commit()

    @staticmethod
    def _refresh_accounting_classification(
        session: Session, *, commit: bool = True
    ) -> None:
        active_link_parents = set(
            session.execute(
                select(TransactionLink.parent_transaction_id).where(
                    TransactionLink.is_active.is_(True),
                    TransactionLink.link_type.in_(AUTO_TRANSACTION_LINK_TYPES),
                )
            ).scalars()
        )
        rows = session.execute(
            select(NormalizedTransaction).where(NormalizedTransaction.is_active.is_(True))
        ).scalars()
        category_types = dict(session.execute(select(Category.id, Category.type)).all())
        for tx in rows:
            tx.accounting_class, tx.accounting_confidence = _accounting_class_for(
                tx,
                tx.id in active_link_parents,
                category_types.get(tx.category_id),
            )
            tx.accounting_version = ACCOUNTING_VERSION
        if commit:
            session.commit()

    @staticmethod
    def _record_source_periods(
        session: Session, df: pd.DataFrame, *, commit: bool = True
    ) -> None:
        """Record observed rows without ever claiming statement completeness."""
        if df.empty:
            return
        working = df.copy()
        working["_month"] = pd.to_datetime(working["booking_date"]).dt.to_period("M")
        for (source, month), group in working.groupby(["source", "_month"]):
            source_value = source if isinstance(source, DataSource) else DataSource(str(source))
            period_start = date(month.year, month.month, 1)
            period_end = date(
                month.year, month.month, calendar.monthrange(month.year, month.month)[1]
            )
            stable_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"k-fin:source-period:{source_value.value}:{period_start}:{period_end}",
                )
            )
            stmt = pg_insert(SourceStatementPeriod).values(
                id=stable_id,
                source=source_value,
                period_start=period_start,
                period_end=period_end,
                rows_present=True,
                observed_row_count=len(group),
                verified_complete=False,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "rows_present": True,
                    "observed_row_count": len(group),
                    # Preserve explicit verification; normalization can only
                    # prove row presence, never statement completeness.
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            session.execute(stmt)
        if commit:
            session.commit()

    def _finish_run(
        self,
        session: Session,
        run_id: str,
        *,
        rows: int,
        error: str | None = None,
        commit: bool = True,
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
        if commit:
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
    """A PayPal credit funding the balance from a bank account — a top-up.

    The PayPal CSV importer deliberately keeps these "Bankgutschrift auf
    PayPal-Konto" rows (see `paypal_csv._is_bank_funding_row`) so this
    matcher can pair them; the canonical `description` carries that German
    `Beschreibung` label verbatim. The deposit is a credit (amount > 0 —
    money arriving in the PayPal balance).
    """
    if _source_value(rec.get("source")) != DataSource.PAYPAL.value:
        return False
    if not rec.get("amount") or rec["amount"] <= 0:
        return False
    # `description` may arrive as a pandas NaN (float) for a row that had
    # none — NaN is truthy, so guard on the type, not on `or ""`.
    desc = rec.get("description")
    return isinstance(desc, str) and BANK_DEPOSIT_LABEL in desc.lower()


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


def _match_paypal_topups(records: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    """Pair PayPal bank-deposit credits with their Comdirect "PAYPAL" debits.

    Returns ``(parent_id, child_id)`` pairs. Each Comdirect posting is consumed
    at most once. Top-ups are not aggregate spend links, but the matched parent
    is excluded from the later PayPal detail-subset matcher.
    """
    pp_deposits = [r for r in records if _is_paypal_bank_deposit(r)]
    cd_paypal = [r for r in records if _is_comdirect_paypal_posting(r)]
    if not pp_deposits or not cd_paypal:
        return []

    candidates: list[tuple[Any, Any]] = []
    for deposit in pp_deposits:
        dep_cents = abs(_amount_cents(deposit["amount"]))
        dep_date = pd.to_datetime(deposit["booking_date"])
        for posting in cd_paypal:
            post_cents = abs(_amount_cents(posting["amount"]))
            if post_cents != dep_cents:
                continue
            if (
                abs((dep_date - pd.to_datetime(posting["booking_date"])).days)
                > INTERNAL_TRANSFER_DAY_WINDOW
            ):
                continue
            candidates.append((posting["id"], deposit["id"]))

    # A date/amount match is evidence only when it identifies both sides
    # uniquely. Greedily consuming the first equal candidate would make input
    # ordering an accounting decision and hide the remaining ambiguity.
    posting_counts: dict[Any, int] = {}
    deposit_counts: dict[Any, int] = {}
    for posting_id, deposit_id in candidates:
        posting_counts[posting_id] = posting_counts.get(posting_id, 0) + 1
        deposit_counts[deposit_id] = deposit_counts.get(deposit_id, 0) + 1
    return [
        (posting_id, deposit_id)
        for posting_id, deposit_id in candidates
        if posting_counts[posting_id] == 1 and deposit_counts[deposit_id] == 1
    ]


def _is_paypal_detail_transaction(rec: dict[str, Any]) -> bool:
    if _source_value(rec.get("source")) != DataSource.PAYPAL.value:
        return False
    if _is_paypal_bank_deposit(rec):
        return False
    return bool(rec.get("amount")) and _amount_cents(rec["amount"]) != 0


def _is_paypal_aggregate_posting(rec: dict[str, Any]) -> bool:
    """A collapsed PayPal debit in a non-PayPal source.

    Comdirect direct debits and Santander card charges can both show only the
    PayPal counterparty while the imported PayPal source contains the merchant
    detail rows.
    """
    if _source_value(rec.get("source")) not in {
        DataSource.COMDIRECT.value,
        DataSource.SANTANDER_CC.value,
    }:
        return False
    if not rec.get("amount") or _amount_cents(rec["amount"]) >= 0:
        return False
    haystack = " ".join(
        str(part) for part in (rec.get("description"), rec.get("recipient")) if part
    ).lower()
    return "paypal" in haystack


def _match_paypal_aggregate_sets(
    records: list[dict[str, Any]], *, excluded_parent_ids: set[Any]
) -> list[dict[str, Any]]:
    """Link collapsed PayPal postings to one unique exact net detail set."""
    parents = sorted(
        (
            r
            for r in records
            if _is_paypal_aggregate_posting(r)
            and r.get("id") not in excluded_parent_ids
        ),
        key=lambda r: (pd.to_datetime(r["booking_date"]), str(r["id"])),
    )
    children = sorted(
        (r for r in records if _is_paypal_detail_transaction(r)),
        key=lambda r: (pd.to_datetime(r["booking_date"]), str(r["id"])),
    )
    if not parents or not children:
        return []

    options: dict[Any, list[frozenset[Any]]] = {}
    for parent in parents:
        parent_date = pd.to_datetime(parent["booking_date"])
        candidates = [
            child
            for child in children
            if abs((parent_date - pd.to_datetime(child["booking_date"])).days)
            <= INTERNAL_TRANSFER_DAY_WINDOW
        ]
        if len(candidates) > MAX_PAYPAL_AGGREGATE_CANDIDATES:
            continue
        subsets = _find_exact_subsets(
            candidates,
            target_cents=_amount_cents(parent["amount"]),
            max_results=MAX_PAYPAL_EXACT_SUBSETS,
        )
        if not subsets:
            continue
        options[parent["id"]] = subsets

    # Resolve all parents and detail sets together.  Only a parent→subset
    # assignment present in every maximum-cardinality one-to-one solution is
    # accepted; competing indistinguishable parents or detail sets therefore
    # remain explicit residuals rather than being decided by iteration order.
    stable = _stable_global_matches(options)
    return [
        {"parent_id": parent_id, "child_ids": sorted(child_ids, key=str)}
        for parent_id, child_ids in sorted(stable.items(), key=lambda item: str(item[0]))
    ]


def _match_santander_cc_settlements(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match Comdirect credit-card settlement debits to Santander billing cycles.

    The billing cycle is the calendar month of the Santander credit-card
    transactions. Its expected settlement is the exact net sum of that month's
    charges and refunds.  The posting date must fall between three days before
    the first cycle row and forty-five days after the last cycle row.  Matching
    is global and one-to-one; ambiguous equal-sum cycles or competing postings
    are left unlinked regardless of input order.
    """
    santander = [
        r
        for r in records
        if _source_value(r.get("source")) == DataSource.SANTANDER_CC.value
    ]
    cd_postings = [r for r in records if _is_comdirect_santander_posting(r)]
    if not santander or not cd_postings:
        return []

    # Bucket the Santander charges by calendar month → net sum + date span.
    buckets: dict[tuple[int, int], dict[str, Any]] = {}
    for rec in santander:
        bdate = pd.to_datetime(rec["booking_date"])
        key = (bdate.year, bdate.month)
        bucket = buckets.setdefault(
            key,
            {
                "sum_cents": 0,
                "min_date": bdate,
                "max_date": bdate,
                "child_ids": [],
            },
        )
        bucket["sum_cents"] += _amount_cents(rec["amount"])
        bucket["min_date"] = min(bucket["min_date"], bdate)
        bucket["max_date"] = max(bucket["max_date"], bdate)
        bucket["child_ids"].append(rec["id"])

    tolerance = pd.Timedelta(days=SANTANDER_SETTLEMENT_DAY_WINDOW)
    max_lag = pd.Timedelta(days=SANTANDER_SETTLEMENT_MAX_LAG_DAYS)
    options: dict[Any, list[frozenset[Any]]] = {}
    for posting in cd_postings:
        post_cents = abs(_amount_cents(posting["amount"]))
        post_date = pd.to_datetime(posting["booking_date"])
        candidates = [
            frozenset(bucket["child_ids"])
            for _key, bucket in sorted(buckets.items())
            if abs(bucket["sum_cents"]) == post_cents
            and post_date >= bucket["min_date"] - tolerance
            and post_date <= bucket["max_date"] + max_lag
        ]
        if candidates:
            options[posting["id"]] = candidates

    stable = _stable_global_matches(options)
    return [
        {"parent_id": parent_id, "child_ids": sorted(child_ids, key=str)}
        for parent_id, child_ids in sorted(stable.items(), key=lambda item: str(item[0]))
    ]


def _find_exact_subsets(
    candidates: list[dict[str, Any]], *, target_cents: int, max_results: int
) -> list[frozenset[Any]]:
    """Return exact non-empty subsets, or none when the safe cap is exceeded."""
    if not candidates:
        return []

    sums: dict[int, list[tuple[int, ...]]] = {0: [()]}
    overflowed: set[int] = set()
    for idx, rec in enumerate(candidates):
        cents = _amount_cents(rec["amount"])
        if cents == 0:
            continue
        additions: dict[int, list[tuple[int, ...]]] = {}
        for current_sum, combos in list(sums.items()):
            next_sum = current_sum + cents
            target = additions.setdefault(next_sum, [])
            for combo in combos:
                candidate_combo = (*combo, idx)
                if candidate_combo not in target:
                    target.append(candidate_combo)
        for next_sum, combos in additions.items():
            existing = sums.setdefault(next_sum, [])
            for combo in combos:
                if combo not in existing:
                    existing.append(combo)
                if len(existing) > max_results:
                    overflowed.add(next_sum)
                    del existing[max_results:]

    if target_cents in overflowed:
        return []
    exact = [combo for combo in sums.get(target_cents, []) if combo]
    return [frozenset(candidates[idx]["id"] for idx in combo) for combo in exact]


def _stable_global_matches(
    options: dict[Any, list[frozenset[Any]]],
) -> dict[Any, frozenset[Any]]:
    """Return assignments common to every maximum one-to-one matching.

    Hyperedges are supported because one PayPal parent may consume several
    detail rows.  Conflicting parent components are solved independently.  A
    defensive state cap fails closed for a pathologically ambiguous component.
    """
    if not options:
        return {}

    remaining = set(options)
    components: list[set[Any]] = []
    while remaining:
        component = {remaining.pop()}
        resource_ids = set().union(
            *(
                child_ids
                for parent in component
                for child_ids in options[parent]
            )
        )
        changed = True
        while changed:
            changed = False
            for parent in list(remaining):
                parent_resources = set().union(*options[parent])
                if resource_ids.intersection(parent_resources):
                    remaining.remove(parent)
                    component.add(parent)
                    resource_ids.update(parent_resources)
                    changed = True
        components.append(component)

    stable: dict[Any, frozenset[Any]] = {}
    for component in components:
        parents = sorted(component, key=str)
        best_count = -1
        consensus: dict[Any, frozenset[Any]] = {}
        states = 0
        overflow = False

        def search(
            index: int,
            used: frozenset[Any],
            assignment: dict[Any, frozenset[Any]],
        ) -> None:
            nonlocal best_count, consensus, states, overflow
            states += 1
            if states > MAX_GLOBAL_MATCHING_STATES:
                overflow = True
                return
            if len(assignment) + len(parents) - index < best_count:
                return
            if index == len(parents):
                count = len(assignment)
                if count > best_count:
                    best_count = count
                    consensus = dict(assignment)
                elif count == best_count:
                    consensus = {
                        parent: child_ids
                        for parent, child_ids in consensus.items()
                        if assignment.get(parent) == child_ids
                    }
                return

            parent = parents[index]
            search(index + 1, used, assignment)
            for child_ids in sorted(
                set(options[parent]), key=lambda ids: tuple(sorted(map(str, ids)))
            ):
                if used.isdisjoint(child_ids):
                    assignment[parent] = child_ids
                    search(index + 1, used.union(child_ids), assignment)
                    assignment.pop(parent)

        search(0, frozenset(), {})
        if not overflow:
            stable.update(consensus)
    return stable


def _find_unique_exact_subset(
    candidates: list[dict[str, Any]], *, target_cents: int
) -> list[dict[str, Any]] | None:
    """Return the only subset summing to target cents; otherwise ``None``."""
    if not candidates:
        return None

    sums: dict[int, list[tuple[int, ...]]] = {0: [()]}
    for idx, rec in enumerate(candidates):
        cents = _amount_cents(rec["amount"])
        if cents == 0:
            continue
        for current_sum, combos in list(sums.items()):
            next_sum = current_sum + cents
            next_combos = sums.setdefault(next_sum, [])
            for combo in combos:
                candidate_combo = (*combo, idx)
                if candidate_combo not in next_combos:
                    next_combos.append(candidate_combo)
                if len(next_combos) > 1:
                    next_combos[:] = next_combos[:2]

    exact = sums.get(target_cents) or []
    if len(exact) != 1:
        return None
    return [candidates[idx] for idx in exact[0]]


def _amount_cents(amount: Any) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))


def _build_transaction_link(
    *, parent_id: Any, child_id: Any, link_type: str
) -> dict[str, Any]:
    stable_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"k-fin:{link_type}:{parent_id}:{child_id}"
    )
    return {
        "id": str(stable_id),
        "parent_transaction_id": str(parent_id),
        "child_transaction_id": str(child_id),
        "link_type": link_type,
    }


def _accounting_class_for(
    tx: NormalizedTransaction,
    has_active_child_links: bool,
    category_type: TypeEnum | str | None = None,
) -> tuple[str, Decimal]:
    """Return one deterministic, mutually-exclusive report-v2 partition.

    Precedence is intentional: transfers/refunds/sign and unresolved aggregate
    parents protect the consolidated boundary first; source-intrinsic financial
    charges then override a possibly stale category; explicit category families
    finally split investments, debt, subscriptions, fixed costs, and remaining
    variable/discretionary consumption.  Recurrence and subscription evidence
    records are deliberately not classification inputs.
    """
    if tx.internal_transfer or tx.category_id in TRANSFER_CATEGORY_IDS:
        confidence = Decimal("1.000") if has_active_child_links else Decimal("0.900")
        return "internal_transfer_settlement_parent", confidence
    if tx.is_refund:
        if tx.amount > 0 and tx.refund_verification_status == "user_verified":
            return "verified_refund_reimbursement", Decimal("1.000")
        return "unresolved_ambiguous", Decimal("0.250")
    if tx.amount >= 0:
        if tx.refund_verification_status == "income_verified":
            return "non_outflow_income", Decimal("1.000")
        return "unresolved_ambiguous", Decimal("0.000")
    candidate = {
        "source": tx.source,
        "amount": tx.amount,
        "description": tx.description,
        "recipient": tx.recipient,
    }
    if (
        _is_paypal_aggregate_posting(candidate)
        or _is_comdirect_santander_posting(candidate)
    ) and not has_active_child_links:
        return "unresolved_ambiguous", Decimal("0.000")
    if tx.category_id in FEE_INTEREST_CATEGORY_IDS:
        return "fee_interest_charge", Decimal("0.950")
    labels = {
        str(value).strip().casefold()
        for value in (tx.description, tx.recipient)
        if value
    }
    if tx.source == DataSource.SANTANDER_CC and labels & FEE_INTEREST_LABELS:
        return "fee_interest_charge", Decimal("1.000")
    if tx.category_id in ASSET_CATEGORY_IDS:
        return "financial_asset_building", Decimal("0.950")
    if tx.category_id in DEBT_CATEGORY_IDS:
        return "debt_principal_financing", Decimal("0.850")
    if tx.category_id in SUBSCRIPTION_CATEGORY_IDS:
        return "subscription_consumption", Decimal("0.900")
    if tx.category_id in FIXED_COST_CATEGORY_IDS or category_type in {
        TypeEnum.FIX,
        TypeEnum.FIX.value,
    }:
        return "fixed_cost_consumption", Decimal("0.900")
    if tx.category_id:
        return "variable_discretionary_consumption", Decimal("0.800")
    return "unresolved_ambiguous", Decimal("0.000")


def refresh_transaction_accounting(
    session: Session, tx: NormalizedTransaction
) -> None:
    """Refresh one active row inside the caller's transaction."""
    if not tx.is_active:
        return
    session.flush()
    has_active_children = (
        session.execute(
            select(func.count())
            .select_from(TransactionLink)
            .where(TransactionLink.parent_transaction_id == tx.id)
            .where(TransactionLink.is_active.is_(True))
            .where(TransactionLink.link_type.in_(AUTO_TRANSACTION_LINK_TYPES))
        ).scalar_one()
        > 0
    )
    category = session.get(Category, tx.category_id) if tx.category_id else None
    tx.accounting_class, tx.accounting_confidence = _accounting_class_for(
        tx,
        has_active_children,
        category.type if category else None,
    )
    tx.accounting_version = ACCOUNTING_VERSION


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
