# Trustworthy analytics

The versioned analytics API answers broad spending questions with an explicit
accounting partition. It does not relabel the legacy `monthly-summary` metric as
total spending.

## Accounting report v2

Only `normalized_transactions.is_active = true` rows participate. Every active
row has exactly one `accounting_class`:

- `internal_transfer_settlement_parent`
- `financial_asset_building`
- `debt_principal_financing`
- `verified_refund_reimbursement`
- `reconciled_consumption`
- `unresolved_ambiguous`
- `non_outflow_income`

The report uses these formulas:

```text
gross cash outflow = abs(sum(active negative rows except
                             internal_transfer_settlement_parent))
reconciled consumption net = reconciled consumption gross - verified refunds
gross cash outflow = financial assets + distinguishable debt
                   + reconciled consumption gross
                   + unresolved outflow residual
```

The report uses a consolidated external-counterparty boundary: linked bank and
card settlement parents are reported separately but are not added to gross cash
outflow. Their merchant/detail children remain the economic outflow, so a
bank → card → PayPal → merchant chain contributes exactly once.

Unmatched or non-unique PayPal/Santander candidates stay in the unresolved
residual. Positive legacy rows remain unresolved until explicitly verified as
income or as a refund, and a negative row can never be verified as a refund.
A positive row reduces consumption only when the refund decision has been
explicitly audited. `gross_cash_outflow` and `reconciled_consumption_net`
are intentionally different facts; neither is exposed as a bare “total spend.”

## Authoritative normalization

Raw rows remain immutable. A correction creates a new raw version and points
the predecessor's `superseded_by` at it. Normalization mirrors that chain with
`normalization_version`, `normalization_status`, `is_active`, and
`superseded_by_id`. No correction deletes either raw or normalized history.

Settlement links are active/versioned records. Exact unique amount/date matches
link bank → PayPal/Santander parent postings to detail children. The aggregate
parent is excluded and merchant children are counted once. A later run marks a
stale auto-link `superseded` and increments its version instead of deleting it.
Existing pre-v2 links are evidence, not truth: the correction pass reruns the
same authoritative matcher and deactivates any legacy link it cannot reproduce.

Matching is global and one-to-one across all candidate parents and detail sets;
only assignments shared by every maximum matching are linked. Santander card
settlements must post no earlier than three days before the first cycle row and
no later than 45 days after the last cycle row. Distant or competing candidates
remain unresolved.

## Safe correction command

After `alembic upgrade head`, preview the count-only plan:

```bash
uv run python scripts/analytics_correction.py
```

Apply only the reported stale-normalized, invalid-link, and classification
version defects:

```bash
uv run python scripts/analytics_correction.py --apply
```

The apply pass is idempotent, stores only result counts in
`analytics_correction_runs`, and performs no deletes. Re-running normalization
restores active state if an operator reverses a raw supersession pointer.

Remitter repair re-ingest uses one PostgreSQL transaction for raw supersession,
successor normalization, link reconciliation, and user-semantic carryover. Any
failure rolls back the entire repair; the same input can then be retried safely.

## Migration downgrade policy

Revision `0028_trustworthy_analytics` cannot be represented losslessly by the
0027 schema. Its downgrade therefore fails closed with a PostgreSQL exception;
it does not drop v2 tables, audit rows, links, decisions, or accounting columns.
The same guard is present in offline Alembic downgrade SQL. Restore an older
application only after a separate, explicitly reviewed compatibility migration
has preserved every v2 evidence field.

## Source completeness and monthly-review UI contract

`SourceStatementPeriod.rows_present` says only that at least one normalized row
was observed. It does not prove a closed statement is complete. The manual UI
workflow is:

Required sources come from the server's `ANALYTICS_REQUIRED_SOURCES` policy;
callers cannot weaken completeness by omitting a source.

1. Call `GET /api/v1/analytics/v2/monthly-review?year=YYYY&month=M`.
2. If state is `missing_source_periods`, show the exact source/month list first.
3. After checking a source statement, a signed-in user records or reverses that
   decision with `PUT /api/v1/analytics/v2/source-periods/verification`.
4. When state becomes `analysis_ready`, render accounting facts, confidence,
   high-impact questions, itemized subscription evidence, and value/leakage
   candidates.

Signed-in users can record evidence through
`PUT /api/v1/analytics/v2/subscriptions/{id}` and
`PUT /api/v1/analytics/v2/value-assessments/{transaction_id}`. These update
local analytical evidence only and never bank state.

The endpoint always returns `scheduler_enabled: false`. This change installs no
cron, reminder, or autonomous bank sync. The React UI lives in the separate
`k-fin-ui` repository; implementing the visual screen there is a separate
release, while this repository owns the workflow state and API contract.

Recurring status is evidence-specific: booked payment, active contract,
projected renewal, variable service, declined charge, mail-only evidence, or a
one-off candidate. Recurrence/category alone creates only `booked_payment`.
Scenario amounts are discrete alternatives, never a price range or contract
truth.

Value assessments use: unavoidable obligation; financial asset building;
durable capability/health/home investment; intentional experiences/joy;
convenience; leakage/waste. Evidence stores declared priority, observed use,
cost/use, duration, duplication, cooling-off regret, and opportunity cost. A
high-impact low-confidence assessment becomes a question rather than a guess.

## Release and deployment discovery

Conventional `feat`/`fix` commits on `main` drive semantic-release through
`.github/workflows/release.yml`. A release builds API/worker images, packages the
Helm chart, and pushes all artifacts to GHCR. An owner may configure a gated
release job to pin the new chart version in a separate GitOps repository.
Repository identity and deployment paths are owner-provided configuration, not
public k-fin defaults. Public/fork installs can use the published OCI chart
directly without a separate GitOps repository.
