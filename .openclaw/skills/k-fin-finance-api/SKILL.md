---
name: k-fin-finance-api
description: Access personal finance data via the k-fin read-only API. Use when asked about bank account balances, transactions, depot positions, or financial overview. Triggers on: account balance, transactions, depot, financial overview, or any question about personal financial data from Comdirect.
---

# k-fin Finance API

Read-only access to normalized financial data (bank, payment, card, and depot).

## Connection

- **Base URL:** `http://localhost:8000` (or `FINANCE_API_URL` env var if set)
- **Auth:** `Authorization: Bearer <FINANCE_API_TOKEN>`
- Finance facts and bank access are GET/read-only. Analytics evidence PUTs are
  user-authenticated local metadata updates; they never mutate a bank.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/v1/transactions` | Active normalized transactions |
| `DELETE /api/v1/categories/{category_id}` | Deletes only an unreferenced category; returns 409 when transaction history or rules reference it |
| `GET /api/v1/aggregates/monthly-summary` | Legacy monthly summary with explicit metric labels |
| `GET /api/v1/analytics/v2/accounting-report` | Versioned accounting partition with unresolved residuals |
| `GET /api/v1/analytics/v2/monthly-review` | Completeness-first monthly review state and facts |
| `PUT /api/v1/analytics/v2/source-periods/verification` | User-only statement verification |
| `PUT /api/v1/analytics/v2/subscriptions/{id}` | User-only itemized recurring-service evidence |
| `PUT /api/v1/analytics/v2/value-assessments/{transaction_id}` | User-only priority/value evidence |

## Workflow

1. Send the Bearer header on every request.
2. Call the narrowest Finance API endpoint that answers the question.
3. For broad monthly analysis, call the v2 monthly-review gate before using facts.

## Important Notes

- The Finance API reads already-normalized local data and never triggers bank writes.
- Never display raw IBANs, full account numbers, or credentials — mask sensitive fields
- Never call observed rows a complete statement. Monthly analysis is valid only
  when `source_completeness.complete` is true.
- Never collapse `gross_cash_outflow` or `economic_consumption_net` into a bare
  “total spending” label. Report the named metric, formula version, confidence,
  and unresolved residuals.
- Keep fixed costs, fees/interest, booked subscriptions, variable/discretionary
  consumption, investments, transfers/settlements, refunds, and uncertainty as
  the separate v2 fields returned by the API.
- Recurring amounts are discrete scenarios. A booked recurrence does not prove
  an active contract or projected renewal.
- See `references/api.md` for the trustworthy analytics response contract.

## Error Handling

- `401` — Token wrong or missing
- `404` — Requested active resource not found
- `409` — Category deletion refused because audit history or rules still reference it
- `422` — Invalid filter or reporting window
- Unreachable — tell the user the Finance API may not be running
