# Gmail Mail Evidence Import

k-fin can import Gmail order, invoice, receipt, payment, and refund mails as
sanitized mail evidence for transaction matching.

## Safety Boundary

- Gmail access is read-only from k-fin's perspective.
- The import script uses `gws` to read Gmail messages.
- k-fin stores only extracted/sanitized evidence rows.
- Raw mail bodies, email addresses, IBANs, and order references are not persisted.
- The script does not archive, label, delete, send, or modify Gmail messages.

## One-Off Import

Run a dry-run first:

```bash
uv run python scripts/import_gmail_evidence.py \
  --dry-run \
  --max-results 25
```

Import into a running local API:

```bash
FINANCE_API_TOKEN=... \
uv run python scripts/import_gmail_evidence.py \
  --api-url http://127.0.0.1:8000 \
  --max-results 25
```

Import into a deployed API:

```bash
FINANCE_API_URL=https://k-fin.example.com \
FINANCE_API_TOKEN=... \
uv run python scripts/import_gmail_evidence.py \
  --max-results 25
```

## Query Tuning

The default Gmail query searches recent finance-like mails:

```text
newer_than:45d (rechnung OR invoice OR quittung OR receipt OR bestellung OR order OR zahlung OR payment OR refund OR erstattung)
```

For a focused run, pass `--query`, for example:

```bash
uv run python scripts/import_gmail_evidence.py \
  --dry-run \
  --query 'newer_than:45d (from:decathlon.de OR from:unzer.com OR from:paypal.de OR rechnung OR invoice)'
```

The script also applies a local quality gate before writing to k-fin:

- empty bodies are skipped
- messages without an extracted amount are skipped
- messages below `--min-confidence` are skipped

## API

`POST /api/v1/mail-evidence/import` accepts the same `MailMessageImport` payload
as the older `/mock-import` endpoint. The service extracts the evidence, upserts
it idempotently, and matches it against transactions in a small date window.
