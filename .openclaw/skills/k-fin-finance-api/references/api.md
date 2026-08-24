# API Response Examples

## Finance API authentication

```http
GET /api/v1/analytics/v2/monthly-review?year=2026&month=1 HTTP/1.1
Host: localhost:8000
Authorization: Bearer <FINANCE_API_TOKEN>
```

## Trustworthy analytics v2

`GET /api/v1/analytics/v2/monthly-review?year=2026&month=1` returns
`state=missing_source_periods` until the exact source period has explicit
statement verification. Required sources are declared by server configuration;
callers cannot omit one. When `state=analysis_ready`, `facts` contains the v2
accounting partition and formulas. Always preserve these response labels:

- `gross_cash_outflow`
- `financial_asset_building_outflow`
- `distinguishable_debt_principal_financing_outflow`
- `fixed_cost_outflow`
- `fee_interest_outflow`
- `subscription_outflow`
- `variable_discretionary_consumption_outflow`
- `economic_consumption_gross`
- `economic_consumption_net`
- `unresolved_ambiguous_outflow_residual`
- `verified_refunds_reimbursements`
- `outflow_partition_total`
- `outflow_partition_difference`

The API intentionally has no field called `total_spending`.

## Category deletion

`DELETE /api/v1/categories/{category_id}` returns `204` only for an unreferenced
category. It returns `409` when any active or inactive normalized transaction,
or a categorization rule, references the category. Reassign active transactions
and rules explicitly; inactive normalization audit history is never rewritten.
