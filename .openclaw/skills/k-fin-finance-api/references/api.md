# API Response Examples

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
