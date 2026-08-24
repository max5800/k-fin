"""Pydantic schemas for the Finance API (M6)."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    kind: str = "expense"
    budgetable: bool = True
    analysis_group: str | None = None
    description: str | None = None
    examples: list[str] | None = None
    anti_examples: list[str] | None = None
    llm_hints: str | None = None


class CategoryCreate(BaseModel):
    id: str | None = None
    name: str
    type: str
    kind: str = "expense"
    budgetable: bool = True
    analysis_group: str | None = None
    description: str | None = None
    examples: list[str] | None = None
    anti_examples: list[str] | None = None
    llm_hints: str | None = None


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category_id: str
    monthly_limit: Decimal
    currency: str
    is_active: bool = True
    priority: int = 0
    warning_threshold: Decimal = Decimal("0.80")
    critical_threshold: Decimal = Decimal("1.00")
    context_note: str | None = None
    category: CategoryOut | None = None


class BudgetUpdate(BaseModel):
    monthly_limit: Decimal
    currency: str = "EUR"
    is_active: bool = True
    priority: int = 0
    warning_threshold: Decimal = Decimal("0.80")
    critical_threshold: Decimal = Decimal("1.00")
    context_note: str | None = None


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class TagCreate(BaseModel):
    id: str | None = None
    name: str


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    external_id: str | None
    booking_date: date
    valuation_date: date
    amount: Decimal
    currency: str
    # FX leg (M16-P2c) — original foreign-currency amount/currency of a
    # non-EUR Santander credit-card purchase; both None for EUR transactions
    # and for every Comdirect/PayPal row.
    original_amount: Decimal | None = None
    original_currency: str | None = None
    sender: str | None
    recipient: str | None
    description: str | None
    category: CategoryOut | None = None
    tags: list[TagOut] = []
    is_recurring: bool
    is_outlier: bool
    internal_transfer: bool
    is_refund: bool
    refund_verification_status: str
    created_at: datetime
    updated_at: datetime


class TransactionListOut(BaseModel):
    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


class TransactionLinkOut(BaseModel):
    id: str
    link_type: str
    transaction: TransactionOut


class TransactionLinksOut(BaseModel):
    transaction_id: str
    children: list[TransactionLinkOut]
    parents: list[TransactionLinkOut]


# ── Mail evidence layer ─────────────────────────────────────────


class MailEvidenceLineItem(BaseModel):
    label: str
    amount: Decimal | None = None
    category_hint: str | None = None


class MailMessageImport(BaseModel):
    """Raw-ish mail input accepted only at the API edge.

    The service extracts structured evidence and discards the body; no raw mail
    content is persisted.
    """

    source: str = "gmail"
    source_message_id: str
    received_at: date | None = None
    sender: str | None = None
    subject: str
    body_text: str


class MailEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    evidence_type: str
    merchant_name: str | None = None
    merchant_key: str | None = None
    document_date: date | None = None
    total_amount: Decimal | None = None
    currency: str = "EUR"
    payment_method: str | None = None
    payment_hint: str | None = None
    order_ref_hash: str | None = None
    subject_hint: str | None = None
    redacted_snippet: str | None = None
    line_items: list[dict] | None = None
    confidence: Decimal
    created_at: datetime
    updated_at: datetime


class EvidenceLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    transaction_id: str
    evidence_id: str
    match_type: str
    confidence: Decimal
    match_reason: str
    created_at: datetime


class MailEvidenceImportOut(BaseModel):
    evidence: MailEvidenceOut
    links: list[EvidenceLinkOut] = []


# ── Categorization rules ────────────────────────────────────────


class RuleOut(BaseModel):
    """A single auto-categorization rule.

    Schema mirrors :class:`src.core.db.models.Rule` and the UI's
    ``CategoryRule`` type — keep field names in lockstep.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    regex_pattern: str
    target_category_id: str
    priority: int


class RuleCreate(BaseModel):
    regex_pattern: str
    target_category_id: str
    priority: int = 0


class RuleUpdate(BaseModel):
    regex_pattern: str | None = None
    target_category_id: str | None = None
    priority: int | None = None


class RulesApplyResult(BaseModel):
    """Response from ``POST /categories/rules/apply-all``.

    Synchronous (HTTP 200) responses populate the count fields.
    Asynchronous (HTTP 202) responses leave them ``None`` and set
    ``status='accepted'`` — the UI polls ``GET /categories/rules`` and
    refetches the transaction list to learn the outcome.

    * ``processed`` — uncategorised transactions scanned.
    * ``matched`` — transactions a rule fired on.
    * ``unchanged`` — scanned rows where no rule matched.
    """

    status: str = "completed"
    processed: int | None = None
    matched: int | None = None
    unchanged: int | None = None


class TransactionUpdate(BaseModel):
    category_id: str | None = None
    tags: list[str] | None = None
    is_refund: bool | None = None
    # Manual override for the heuristic in `_flag_internal_transfers`. The
    # detector only catches paired transfers when both legs hit our DB; a
    # one-off DKB-/Tagesgeld-Outflow needs the user to flag it themselves.
    internal_transfer: bool | None = None
    # Marks/un-marks the row as audited. `True` stamps `refund_audit_decided_at`
    # to now() — the row drops out of /aggregates/refund-audit. `False` clears
    # the timestamp (re-surface for review). Independent of is_refund: marking
    # as refund auto-stamps it backend-side; the explicit field is for the
    # "Echtes Einkommen" path that doesn't otherwise touch the row.
    refund_audit_decided: bool | None = None


# ── Runs API (M6) ────────────────────────────────────────────────


class RunCreate(BaseModel):
    trigger: str = "manual"


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_name: str
    status: str
    trigger: str
    result: dict | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_message: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    usage_detail: dict | None = None
    # M11 reliability surface — surfaced live while status='running'.
    heartbeat_at: datetime | None = None
    last_error: str | None = None


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


class RunHealthPendingSource(BaseModel):
    source: str
    pending: int


class RunHealthOut(BaseModel):
    window_days: int
    threshold: float
    runs_total: int
    suggestions_total: int
    high_confidence_total: int
    auto_apply_rate: float | None = None
    avg_confidence: float | None = None
    memory_batches_total: int
    memory_batches_with_hits: int
    memory_hit_rate: float | None = None
    memory_hits_total: int
    low_conf_with_memory: int
    low_conf_without_memory: int
    pending_by_source: list[RunHealthPendingSource]
    pending_total: int


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


class CategoryBreakdown(BaseModel):
    category_id: str
    category_name: str
    total: Decimal
    transaction_count: int


class MonthlySummaryOut(BaseModel):
    year: int
    month: int
    income: Decimal
    expenses: Decimal
    net: Decimal
    # Savings rate as a fraction in [-inf, 1.0]: 0.30 means 30 %.
    # API convention: all `_rate` / `_ratio` fields are fractions, never percent values.
    savings_rate: Decimal
    transaction_count: int
    by_category: list[CategoryBreakdown]


class CashflowPoint(BaseModel):
    year: int
    month: int
    income: Decimal
    expenses: Decimal
    net: Decimal
    transaction_count: int


class CashflowOverTimeOut(BaseModel):
    series: list[CashflowPoint]
    total_months: int


class BudgetSpendingItem(BaseModel):
    category_id: str
    category_name: str
    monthly_limit: Decimal
    currency: str
    # `spent_gross` is the negative-amount sum (original expenses).
    # `refunded` is the positive sum of `is_refund=True` transactions in
    # the same category. `spent_net = spent_gross + refunded` (still negative
    # if there is residual spend). `remaining = monthly_limit + spent_net`.
    spent_gross: Decimal
    refunded: Decimal
    spent_net: Decimal
    remaining: Decimal
    transaction_count: int
    utilization: Decimal | None = None
    status: str | None = None
    priority: int | None = None
    context_note: str | None = None


class BudgetSpendingOut(BaseModel):
    year: int
    month: int
    items: list[BudgetSpendingItem]


class AnalysisContextOut(BaseModel):
    year: int
    month: int
    period_start: str
    period_end: str
    generated_at: str
    monthly_summary: dict
    budget_spending: dict
    budget_risks: list[dict]
    uncategorized_count: int
    category_semantics: list[dict]
    mail_evidence: list[dict]
    top_transactions: list[dict]
    assumptions: list[str]


class SourcePeriodVerificationIn(BaseModel):
    source: str
    period_start: date
    period_end: date
    verified_complete: bool
    verification_method: str


class SourcePeriodStateOut(BaseModel):
    source: str
    period_start: str
    period_end: str
    state: str
    rows_present: bool
    observed_row_count: int
    verified_complete: bool
    verification_method: str | None


class SourceCompletenessOut(BaseModel):
    complete: bool
    periods: list[SourcePeriodStateOut]
    missing_periods: list[dict[str, str]]


class TrustworthyAccountingReportOut(BaseModel):
    report_version: int
    period_start: str
    period_end: str
    transaction_count: int
    gross_cash_outflow: Decimal
    internal_transfer_and_settlement_parent_outflow: Decimal
    financial_asset_building_outflow: Decimal
    distinguishable_debt_principal_financing_outflow: Decimal
    fixed_cost_outflow: Decimal
    fee_interest_outflow: Decimal
    subscription_outflow: Decimal
    variable_discretionary_consumption_outflow: Decimal
    verified_refunds_reimbursements: Decimal
    economic_consumption_gross: Decimal
    economic_consumption_net: Decimal
    unresolved_ambiguous_outflow_residual: Decimal
    unresolved_ambiguous_inflow_residual: Decimal
    non_outflow_income: Decimal
    outflow_partition_total: Decimal
    outflow_partition_difference: Decimal
    confidence: str
    minimum_classification_confidence: Decimal
    formulas: dict[str, str]


class MonthlyReviewOut(BaseModel):
    workflow_version: int
    year: int
    month: int
    state: str
    can_analyze: bool
    source_completeness: SourceCompletenessOut
    scheduler_enabled: bool
    scheduler_note: str
    facts: TrustworthyAccountingReportOut | None
    confidence: str
    high_impact_questions: list[dict]
    value_review: dict | None
    subscriptions: list[dict]


class SubscriptionRecordIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    status: Literal[
        "booked_payment",
        "active_contract",
        "projected_renewal",
        "variable_service",
        "declined_charge",
        "mail_only_evidence",
        "one_off_candidate",
    ]
    confidence: Decimal = Field(ge=0, le=1)
    evidence_source: str = Field(min_length=1, max_length=32)
    transaction_id: str | None = None
    amount_scenarios: list[Decimal] = Field(default_factory=list)
    next_review_date: date | None = None


class ValueAssessmentIn(BaseModel):
    value_class: Literal[
        "unavoidable_obligation",
        "financial_asset_building",
        "durable_capability_health_home_investment",
        "intentional_experience_joy",
        "convenience",
        "leakage_waste",
    ]
    confidence: Decimal = Field(ge=0, le=1)
    declared_priority: int | None = Field(default=None, ge=0, le=100)
    observed_use_count: int | None = Field(default=None, ge=0)
    cost_per_use: Decimal | None = Field(default=None, ge=0)
    duration_months: int | None = Field(default=None, ge=0)
    duplication: bool | None = None
    cooling_off_regret: bool | None = None
    opportunity_cost: Decimal | None = Field(default=None, ge=0)
    question: str | None = Field(default=None, max_length=300)


class RefundAuditCandidate(BaseModel):
    """A positive-amount transaction sitting in `erstattungen` with
    `is_refund=False` — i.e. either a real income (Steuer, Cashback) or
    an actual refund that the user hasn't reclassified yet.

    `suggested_category_id` is a heuristic-based hint built from the
    sender / recipient / description; the UI shows it as the default in
    the category picker but the user always has the final say.
    """

    id: str
    booking_date: date
    amount: Decimal
    sender: str | None
    recipient: str | None
    description: str | None
    suggested_category_id: str | None
    suggested_reason: str | None


class RefundAuditOut(BaseModel):
    candidates: list[RefundAuditCandidate]
    total: int


class RefundAutoApplyResult(BaseModel):
    """Counts produced by `apply_refund_heuristic` — surfaced by the
    `/aggregates/refund-audit/auto-apply` endpoint and logged on startup.
    """

    applied_refund: int
    applied_income: int
    left_for_review: int


# ── Reports API (M6) ────────────────────────────────────────────


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    report_type: str
    title: str
    period_start: date
    period_end: date
    format: str
    size_bytes: int | None
    status: str
    error: str | None
    # Agent-produced JSON payload (CategorizationResult, AnalysisResult, …).
    content: dict | None = None
    created_at: datetime
    updated_at: datetime


class ReportListOut(BaseModel):
    items: list[ReportOut]
    total: int
    limit: int
    offset: int


# ── Portfolio / Depot (M8) ─────────────────────────────────────


class DepotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    depot_id: str
    depot_type: str | None = None
    currency: str
    total_value: Decimal
    total_purchase_value: Decimal
    total_pnl_abs: Decimal
    total_pnl_rel: Decimal
    positions_count: int
    last_synced_at: datetime | None = None


class InstrumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    isin: str
    wkn: str | None = None
    name: str
    instrument_type: str | None = None
    currency: str
    ticker_symbol: str | None = None


class InstrumentPatch(BaseModel):
    """User-editable fields on an :class:`Instrument`.

    Only ``ticker_symbol`` is currently mutable — name/type/currency
    flow from Comdirect and would be overwritten on next sync.
    """

    ticker_symbol: str | None = None


# 5 calendar years (rounded up to 1827 days to absorb leap-year wobble) is the
# upper bound a single backfill request may span. yfinance throttles by IP and
# rejects long windows on illiquid tickers; without a cap the worker can be
# pinned for minutes on a 1900-2030 request and risk an IP-level ban.
_BACKFILL_MAX_DAYS = 5 * 365 + 2


class PriceBackfillRequest(BaseModel):
    from_date: date
    to_date: date

    @model_validator(mode="after")
    def _check_window(self) -> "PriceBackfillRequest":
        # Note: the *order* of from_date vs to_date is enforced in the
        # router (HTTP 400 with a friendly message). We only cap the
        # *width* here, since that's a pure schema concern that needs
        # to fire regardless of caller. An inverted range falls through
        # this check (negative delta is < cap) and the router's own
        # check picks it up downstream.
        if self.to_date >= self.from_date:
            delta = self.to_date - self.from_date
            if delta > timedelta(days=_BACKFILL_MAX_DAYS):
                raise ValueError(
                    "Backfill window must not exceed 5 years "
                    f"(got {delta.days} days, max {_BACKFILL_MAX_DAYS})"
                )
        return self


class PriceBackfillResult(BaseModel):
    isin: str
    ticker_symbol: str
    requested_from: date
    requested_to: date
    fetched_points: int
    inserted_points: int
    skipped_existing: int
    source: str = "yfinance"


class InstrumentPricePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price_date: date
    close: Decimal
    currency: str
    source: str


class PositionOut(BaseModel):
    depot_id: str
    instrument: InstrumentOut
    quantity: Decimal
    current_price: Decimal
    current_value: Decimal
    purchase_value: Decimal
    prev_day_price: Decimal | None = None
    daily_pnl_abs: Decimal
    daily_pnl_rel: Decimal
    total_pnl_abs: Decimal
    total_pnl_rel: Decimal
    weight_pct: Decimal
    currency: str
    as_of: datetime


class DepotTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    depot_id: str
    isin: str | None = None
    booking_date: date
    transaction_type: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    currency: str


class DepotTransactionListOut(BaseModel):
    items: list[DepotTransactionOut]
    total: int
    limit: int
    offset: int


class PortfolioSummaryOut(BaseModel):
    total_value: Decimal
    total_purchase_value: Decimal
    total_pnl_abs: Decimal
    total_pnl_rel: Decimal
    daily_pnl_abs: Decimal
    daily_pnl_rel: Decimal
    dividend_yield_pct: Decimal
    positions_count: int
    depots_count: int
    last_synced_at: datetime | None = None


class AllocationBucketOut(BaseModel):
    bucket: str
    value: Decimal
    share_pct: Decimal


class PerformancePointOut(BaseModel):
    snapshot_date: date
    total_value: Decimal
    total_purchase_value: Decimal


class PortfolioActivityOut(BaseModel):
    transaction_id: str
    depot_id: str
    isin: str | None = None
    instrument_name: str | None = None
    instrument_type: str | None = None
    booking_date: date
    transaction_type: str
    quantity: Decimal
    price: Decimal
    amount: Decimal
    currency: str


class PortfolioActivityListOut(BaseModel):
    items: list[PortfolioActivityOut]
    total: int
    limit: int
    offset: int


class PortfolioHomeOut(BaseModel):
    summary: PortfolioSummaryOut
    allocation: list[AllocationBucketOut]
    performance: list[PerformancePointOut]
    activities: list[PortfolioActivityOut]


class SavingsPlanUpdate(BaseModel):
    amount: Decimal
    currency: str = "EUR"
    interval: str = "monthly"
    start_date: date
    active: bool = True
    note: str | None = None


class SavingsPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    isin: str
    amount: Decimal
    currency: str
    interval: str
    start_date: date
    active: bool
    note: str | None = None
    instrument: InstrumentOut | None = None


class PortfolioTargetUpdate(BaseModel):
    target_weight_pct: Decimal
    max_weight_pct: Decimal | None = None
    active: bool = True
    note: str | None = None


class PortfolioTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_type: str
    target_key: str
    target_weight_pct: Decimal
    max_weight_pct: Decimal | None = None
    active: bool
    note: str | None = None


class PortfolioPlanPositionOut(BaseModel):
    isin: str
    name: str
    current_value: Decimal
    current_weight_pct: Decimal
    target_weight_pct: Decimal | None = None
    target_gap_pct: Decimal | None = None
    active_savings_amount: Decimal | None = None
    max_weight_pct: Decimal | None = None
    over_single_position_cap: bool
    looks_like_etf: bool


class PortfolioPlanSuggestionOut(BaseModel):
    severity: str
    action: str
    isin: str | None = None
    label: str
    detail: str


class PortfolioPlanReportOut(BaseModel):
    total_value: Decimal
    active_monthly_savings_amount: Decimal
    positions: list[PortfolioPlanPositionOut]
    targets: list[PortfolioTargetOut]
    suggestions: list[PortfolioPlanSuggestionOut]


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    # Upstream provider the run ingested (comdirect | paypal | …). Null
    # for normalization passes, which are source-agnostic.
    data_source: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    rows_processed: int
    error: str | None = None


class LastSyncOut(BaseModel):
    """Most recent raw-import run for one data source — drives the
    per-source "Last sync" indicator in the UI TopBar."""

    model_config = ConfigDict(from_attributes=True)

    data_source: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
