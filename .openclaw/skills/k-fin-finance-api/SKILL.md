---
name: k-fin-finance-api
description: Access personal finance data via the k-fin read-only API. Use when asked about bank account balances, transactions, depot positions, or financial overview. Triggers on: account balance, transactions, depot, financial overview, or any question about personal financial data from Comdirect.
---

# k-fin Finance API

Read-only access to exported financial data (Comdirect bank + depot).

## Connection

- **Base URL:** `http://localhost:8001` (or `COMDIRECT_API_URL` env var if set)
- **Export auth:** Query param `?token=<COMDIRECT_API_TOKEN>` (stored in OpenClaw config)
- **Finance API auth:** `Authorization: Bearer <user JWT>`; owner-scoped reports reject service tokens
- **All endpoints are GET, read-only**

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /exports?token=...` | All available CSV export files (filename, size, modified) |
| `GET /exports/latest?token=...` | Most recent file per category |
| `GET /exports/{filename}?token=...` | Download a specific CSV file |
| `GET /api/v1/reporting/accounting` | Owner-scoped, provisional accounting partition and source freshness |

## Workflow

1. Call `/exports/latest` to see what is available and get filenames
2. Download the relevant CSV with `/exports/{filename}`
3. Parse CSV: **semicolon-delimited**, UTF-8-sig, German number/date formats (e.g. `1.234,56` = 1234.56)

For accounting facts, call `/api/v1/reporting/accounting` with a user JWT,
`date_from`, `date_to`, deterministic `as_of`, and every declared `sources`
value. Do not use the static service token: the endpoint requires an
authenticated owner. Treat `analysis_state=incomplete_sources` as blocking and
never describe `source_coverage.complete=false` as a complete statement period.

## Export Categories

| Prefix | Content |
|---|---|
| `umsaetze_` | Account transactions (Girokonto) |
| `depot_positionen_` | Current depot positions (securities) |
| `depot_umsaetze_` | Depot transactions (buys/sells) |
| `finanzuebersicht_` | Financial overview (accounts + depot combined) |

## Important Notes

- The API only serves **already exported** CSVs — it does NOT trigger new exports from Comdirect
- The accounting endpoint is read-only and only sees active rows carrying an
  explicit `raw_data.owner_user_id` matching the caller; unattributed rows are
  omitted rather than assigned heuristically
- A settlement parent is excluded only when its persisted link chain is unique,
  source-valid, and exact-sum; inspect `settlement_ambiguities` before using totals
- Fresh rows are not proof of statement completeness; `can_claim_complete` is
  deliberately false with the current schema
- If data is stale, the export job must be run manually: `uv run python scripts/export_csv.py`
- Never display raw IBANs, full account numbers, or credentials — mask sensitive fields
- See `references/api.md` for full response format and CSV parsing examples

## Error Handling

- `401` — Token wrong or missing
- `404` — File not found (export may not have run yet)
- `400` — Invalid filename
- Unreachable — tell the user the export service may not be running
