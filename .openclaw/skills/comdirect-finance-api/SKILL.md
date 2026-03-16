---
name: comdirect-finance-api
description: Access personal finance data via the comdirect-firefly-sync read-only API. Use when asked about bank account balances, transactions, depot positions, or financial overview. Triggers on: account balance, transactions, depot, financial overview, or any question about personal financial data from Comdirect.
---

# Comdirect Finance API

Read-only access to exported financial data (Comdirect bank + depot).

## Connection

- **Base URL:** `http://localhost:8001` (or `COMDIRECT_API_URL` env var if set)
- **Auth:** Query param `?token=<COMDIRECT_API_TOKEN>` (stored in OpenClaw config)
- **All endpoints are GET, read-only**

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /exports?token=...` | All available CSV export files (filename, size, modified) |
| `GET /exports/latest?token=...` | Most recent file per category |
| `GET /exports/{filename}?token=...` | Download a specific CSV file |

## Workflow

1. Call `/exports/latest` to see what is available and get filenames
2. Download the relevant CSV with `/exports/{filename}`
3. Parse CSV: **semicolon-delimited**, UTF-8-sig, German number/date formats (e.g. `1.234,56` = 1234.56)

## Export Categories

| Prefix | Content |
|---|---|
| `umsaetze_` | Account transactions (Girokonto) |
| `depot_positionen_` | Current depot positions (securities) |
| `depot_umsaetze_` | Depot transactions (buys/sells) |
| `finanzuebersicht_` | Financial overview (accounts + depot combined) |

## Important Notes

- The API only serves **already exported** CSVs — it does NOT trigger new exports from Comdirect
- If data is stale, the export job must be run manually: `uv run python scripts/export_csv.py`
- Never display raw IBANs, full account numbers, or credentials — mask sensitive fields
- See `references/api.md` for full response format and CSV parsing examples

## Error Handling

- `401` — Token wrong or missing
- `404` — File not found (export may not have run yet)
- `400` — Invalid filename
- Unreachable — tell the user the export service may not be running
