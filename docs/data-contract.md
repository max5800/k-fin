# Data Contract — Comdirect Finance Data Platform

> Version 0.1.0 · 2026-04-10
> Source of truth: `src/connector/models.py`

This document defines the canonical data shapes produced by the platform.
All downstream consumers (CSV exports, JSON exports, APIs, future importers)
MUST derive their output from the Pydantic models listed here.

---

## 1. Entities

### 1.1 Account (`ComdirectAccount`)

Represents a bank account (Girokonto, Tagesgeld, Verrechnungskonto).

| Field | Type | Nullable | Source | Stability |
|-------|------|----------|--------|-----------|
| `account_id` | `str` | no (default `""`) | `account.accountId` | **stable** — primary key in Comdirect |
| `iban` | `str` | no (default `""`) | `account.iban` | **stable** |
| `account_type` | `str` | no (default `""`) | `account.accountType.key` | **stable** — see §3 Enums |
| `balance` | `float` | no (default `0.0`) | `balance.value` | **volatile** — changes with every booking |
| `currency` | `str` | no (default `"EUR"`) | `balance.unit` | **stable** |

### 1.2 Transaction (`ComdirectTransaction`)

Represents a bank account transaction (Umsatz).

| Field | Type | Nullable | Source | Stability |
|-------|------|----------|--------|-----------|
| `transaction_id` | `str` | no (default `""`) | `transactionId` | **stable** — primary key |
| `booking_date` | `str` | no (default `""`) | `bookingDate` | **stable** — YYYY-MM-DD |
| `value_date` | `str` | no (default `""`) | `valutaDate` | **stable** — YYYY-MM-DD |
| `amount` | `float` | no (default `0.0`) | `transactionValue.value` | **stable** — negative = debit |
| `currency` | `str` | no (default `"EUR"`) | `transactionValue.unit` | **stable** |
| `type_text` | `str` | no (default `""`) | `typeText` | **fragile** — free-text from Comdirect, may change wording |
| `remittance_info` | `str` | no (default `""`) | `remittanceInfo` | **fragile** — free-text |
| `creditor_name` | `str` | no (default `""`) | `creditor.holderName` | **stable** when present |
| `creditor_iban` | `str` | no (default `""`) | `creditor.iban` | **stable** when present |
| `debtor_name` | `str` | no (default `""`) | `debtor.holderName` | **stable** when present |
| `debtor_iban` | `str` | no (default `""`) | `debtor.iban` | **stable** when present |

**Derived properties** (computed, not stored):

| Property | Logic | Notes |
|----------|-------|-------|
| `is_debit` | `amount < 0` | |
| `counterpart_name` | creditor if debit, debtor if credit | |
| `counterpart_iban` | creditor IBAN if debit, debtor IBAN if credit | |
| `description` | `remittance_info or type_text or "Umsatz"`, max 255 chars | Truncation is a Firefly constraint carried over |

### 1.3 Depot Position (`DepotPosition`)

Represents a current securities holding.

| Field | Type | Nullable | Source | Stability |
|-------|------|----------|--------|-----------|
| `isin` | `str` | no (default `""`) | `instrument.isin` | **stable** — international standard |
| `wkn` | `str` | no (default `""`) | `instrument.wkn` | **stable** — German WKN |
| `name` | `str` | no (default `""`) | `instrument.name` | **fragile** — display name, may change |
| `quantity` | `float` | no (default `0.0`) | `quantity.value` or `stueckzahl` | **volatile** — changes with trades |
| `current_price` | `float` | no (default `0.0`) | `currentPrice.value` or `kurs.value` | **volatile** — market price |
| `current_value` | `float` | no (default `0.0`) | `currentValue.value` or `kurswert.value` | **volatile** — qty × price |
| `purchase_value` | `float` | no (default `0.0`) | `purchaseValue.value` or `einstandswert.value` | **semi-stable** — changes with new buys |
| `currency` | `str` | no (default `"EUR"`) | `currentValue.unit` or `kurswert.unit` | **stable** |

**Derived properties:**

| Property | Logic |
|----------|-------|
| `gains` | `current_value - purchase_value` (rounded to 2 decimals) |
| `gains_percent` | `gains / purchase_value * 100` or `0` if no cost basis |

**Note:** The model accepts both modern camelCase API fields and legacy German field names (`stueckzahl`, `kurs`, `kurswert`, `einstandswert`). This dual mapping is intentional — the Comdirect API has historically returned both shapes.

### 1.4 Depot Transaction (`DepotTransaction`)

Represents a securities transaction (buy, sell, dividend).

| Field | Type | Nullable | Source | Stability |
|-------|------|----------|--------|-----------|
| `transaction_id` | `str` | no (default `""`) | `transactionId` | **stable** — primary key |
| `booking_date` | `str` | no (default `""`) | `bookingDate` or `transactionDate` | **stable** — YYYY-MM-DD |
| `isin` | `str` | no (default `""`) | `instrument.isin` | **stable** |
| `wkn` | `str` | no (default `""`) | `instrument.wkn` | **stable** |
| `name` | `str` | no (default `""`) | `instrument.name` | **fragile** |
| `transaction_type` | `str` | no (default `""`) | Normalized — see §3 | **stable** after normalization |
| `quantity` | `float` | no (default `0.0`) | `quantity.value` or `stueckzahl` | **stable** |
| `price` | `float` | no (default `0.0`) | `price.value` or `kurs.value` | **stable** |
| `amount` | `float` | no (default `0.0`) | `transactionValue.value` or `amount.value` | **stable** |
| `currency` | `str` | no (default `"EUR"`) | `transactionValue.unit` or `amount.unit` | **stable** |

### 1.5 Complete Dataset (`ComdirectData`)

Top-level container returned by `ComdirectClient.get_all_data()`.

| Field | Type | Key |
|-------|------|-----|
| `accounts` | `list[ComdirectAccount]` | — |
| `transactions` | `dict[str, list[ComdirectTransaction]]` | keyed by `account_id` |
| `depots` | `list[dict]` | raw depot objects (untyped) |
| `depot_positions` | `dict[str, list[DepotPosition]]` | keyed by `depot_id` |
| `depot_transactions` | `dict[str, list[DepotTransaction]]` | keyed by `depot_id` |

**Known gap:** `depots` is `list[dict]` — no Pydantic model exists for the depot object itself. Consider adding a `ComdirectDepot` model with at least `depot_id` and `account_id`.

### 1.6 Financial Overview (export-only)

The financial overview is not a model — it is a **derived CSV** constructed at export time in `scripts/export_csv.py`. It aggregates account balances and depot values into a summary.

| Column | Source |
|--------|--------|
| `Typ` | `"Konto"` or `"Depot"` |
| `Bezeichnung` | Account type display name |
| `IBAN / Depot-Nr.` | Account IBAN or depot ID |
| `Saldo (EUR)` | Account balance or sum of position values |

---

## 2. Export Formats

### 2.1 CSV Exports (`scripts/export_csv.py`)

German-locale CSVs matching Comdirect's own download format.

| File | Columns |
|------|---------|
| `umsaetze_<IBAN>_<dates>.csv` | Buchungstag, Wertstellung (Valuta), Vorgang, Buchungstext, Umsatz in EUR |
| `depot_positionen_<date>.csv` | ISIN, WKN, Bezeichnung, Stückzahl, Kurs, Kurs-Währung, Aktueller Wert (EUR), Einstandswert (EUR), Gewinn/Verlust (EUR), Gewinn/Verlust (%) |
| `depot_umsaetze_<dates>.csv` | Buchungstag, Geschäftsart, ISIN, WKN, Bezeichnung, Stückzahl, Kurs, Betrag (EUR) |
| `finanzuebersicht_<date>.csv` | Typ, Bezeichnung, IBAN / Depot-Nr., Saldo (EUR) |

Formatting: dates as `DD.MM.YYYY`, amounts with comma decimal separator, CSV with semicolon delimiter.

### 2.2 Finance Agent JSON (`scripts/export_finance_data.py`)

Structured JSON for downstream AI/automation consumers.

```jsonc
{
  "girokonto":           { "account_id", "transactions": [...], "summary": {...} },
  "tagesgeld":           { "account_id", "transactions": [...], "summary": {...} },
  "verrechnungskonto":   { "account_id", "transactions": [...], "summary": {...} },
  "depot": {
    "depot_id",
    "positions":    [...],
    "transactions": [...],
    "summary":      { "total_value", "total_purchase_value", "total_gains", ... }
  },
  "meta": { "exported_at", "account_count", "total_transaction_count" }
}
```

Account type mapping (from `accountType.key`):

| API Key | Canonical Name |
|---------|---------------|
| `CURRENT_ACCOUNT`, `GIRO` | `girokonto` |
| `SAVINGS_ACCOUNT`, `TAGESGELD` | `tagesgeld` |
| `CLEARING_ACCOUNT`, `DEPOT_VERRECHNUNGSKONTO` | `verrechnungskonto` |

### 2.3 REST API (`src/api/serve_exports.py`)

Read-only API serving exported files. No transformation — serves raw CSVs.

In the Kubernetes deployment, the API (`comdirect-api`, port 8000) is the public-facing service. Sync is triggered via the API, which calls the internal `comdirect-worker` (port 8001) to perform the actual Comdirect data export.

| Endpoint | Response |
|----------|----------|
| `GET /health` | `{"status": "ok"}` |
| `GET /exports` | File listing with metadata |
| `GET /exports/latest` | Latest file per category |
| `GET /exports/{filename}` | Raw CSV download (`text/csv; charset=utf-8-sig`) |

---

## 3. Enums & Constants

### Account Types

| Comdirect API Value | Normalized |
|---------------------|-----------|
| `CURRENT_ACCOUNT` | Girokonto |
| `GIRO` | Girokonto (legacy) |
| `SAVINGS_ACCOUNT` | Tagesgeld |
| `TAGESGELD` | Tagesgeld (legacy) |
| `CLEARING_ACCOUNT` | Verrechnungskonto |
| `DEPOT_VERRECHNUNGSKONTO` | Verrechnungskonto |

### Depot Transaction Types

| Raw API Values | Normalized |
|----------------|-----------|
| `BUY`, `IN`, `KAUF` | `BUY` |
| `SELL`, `OUT`, `VERKAUF` | `SELL` |
| `DIVIDEND`, `ERTRAG` | `DIVIDEND` |
| *(anything else)* | `OTHER` |

---

## 4. Stability Assessment

| Category | Assessment |
|----------|-----------|
| **Stable fields** | IDs, IBANs, ISINs, WKNs, dates, amounts, currencies — these are primary keys or immutable financial facts |
| **Semi-stable fields** | `purchase_value` (changes with new buys), `account_type` (unlikely to change but not guaranteed) |
| **Volatile fields** | `balance`, `current_price`, `current_value`, `quantity` — point-in-time snapshots |
| **Fragile fields** | `type_text`, `remittance_info`, `name` (instrument) — free-text from the API, may change wording between API versions |
| **Dual-format parsing** | Position/depot-transaction models accept both camelCase and German field names — this is load-bearing, not tech debt |

---

## 5. Recommendations

### 5.1 Add Normalized JSON Export (recommended next step)

The Finance Agent JSON format (`scripts/export_finance_data.py`) is close to a canonical normalized export but has a few gaps:

1. **Export from Pydantic models, not raw dicts.** The mapper currently receives raw dicts from `get_all_data()`. It should consume `ComdirectData` (which already parses everything) and use `.model_dump()` for serialization. This would eliminate the duplicate field-extraction logic in the mapper.

2. **Add a `ComdirectDepot` model.** The `depots` field in `ComdirectData` is `list[dict]` — the only untyped entity. A minimal model (`depot_id: str`, `account_id: str`) would close this gap.

3. **Expose JSON via the REST API.** Add a `GET /exports/latest/json` endpoint that returns the Finance Agent JSON directly, so consumers don't need to run the CLI script.

### 5.2 Do NOT Change

- **Manual pushTAN flow** — deliberate security boundary, not a limitation.
- **Dual camelCase / German field parsing** — the Comdirect API has returned both shapes historically; removing either branch risks breakage.
- **CSV German formatting** — matches Comdirect's own export format, useful for manual comparison.
- **Read-only access** — fundamental security constraint, never add write operations.

### 5.3 Consider Later

- **Versioned schema** — if the export format is consumed by external systems, add a `schema_version` field to JSON exports.
- **Strict enums** — replace `str` account types and transaction types with `Enum` classes for compile-time safety.
- **Timestamp standardization** — dates are currently `str` in YYYY-MM-DD format. Consider `datetime.date` in models, with string serialization only at the export boundary.
