# API Response Examples

## GET /api/v1/reporting/accounting

Requires a user JWT. Example query:

```text
/api/v1/reporting/accounting?date_from=2026-08-01&date_to=2026-08-31&as_of=2026-08-26&sources=comdirect&sources=paypal
```

The response partitions every owner-attributed active transaction into exactly
one class. Each class contains `transaction_count`, `outflow`, `inflow`, and
`net`; `partition_difference` must be zero. `settlement_ambiguities` contains
transaction IDs and machine-readable reasons for any link chain the API could
not uniquely prove.

`source_coverage.sources` reports `fresh`, `stale`, or `missing` per declared
source. A stale or missing manual source makes
`analysis_state=incomplete_sources`. The existing schema has no statement-period
verification record, so `source_coverage.complete` and `can_claim_complete`
remain false even when row freshness is good.

## GET /exports/latest

```json
{
  "latest": {
    "umsaetze": {
      "label": "Konto-Umsaetze",
      "filename": "umsaetze_2026-03-16.csv",
      "size_bytes": 12400,
      "modified": 1773600000.0
    },
    "depot_positionen": {
      "label": "Depot-Positionen",
      "filename": "depot_positionen_2026-03-16.csv",
      "size_bytes": 3200,
      "modified": 1773600000.0
    },
    "finanzuebersicht": {
      "label": "Finanzuebersicht",
      "filename": "finanzuebersicht_2026-03-16.csv",
      "size_bytes": 800,
      "modified": 1773600000.0
    }
  }
}
```

## CSV Format

Semicolon-delimited, UTF-8-sig encoding, German locale:

```
Buchungstag;Valutadatum;Vorgang;Buchungstext;Umsatz in EUR
16.03.2026;16.03.2026;Lastschrift;REWE Stegaurach;-45,30
15.03.2026;15.03.2026;Gutschrift;Arbeitgeber GmbH;3.200,00
```

**Parsing numbers:** Remove `.` thousand separator, replace `,` decimal with `.`
Example: `3.200,00` -> `3200.00`

**Parsing dates:** Format `DD.MM.YYYY`
