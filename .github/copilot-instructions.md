# Copilot Instructions — k-fin

## Project

k-fin — Personal Finance Intelligence Platform. Read-only financial data export from the Comdirect REST API — as CSV, Finance API, and normalization pipeline into Postgres.

## Identity

`k-fin` resolves internally to "Klaus Fin" (fish-fin). Klaus is the user's OpenClaw AI assistant — a talking goldfish, former GDR figure-skating champion. k-fin is his finance workbench. **Always write the product as `k-fin`** (lowercase, hyphenated) — never spell out "Klaus Finanzen" or "Klaus Finance" in code, UI, logs, commits, or docs. Banking code stays serious; identity stays subtle.

## Architecture

- `src/connector/` — Comdirect API client (OAuth2 + pushTAN auth, accounts, transactions, depot)
- `src/api/` — K-Fin Finance API (FastAPI, port 8000) with routers for transactions, categories, tags, aggregates, runs, reports, sync
- `src/importer/` — ~~Firefly III client + transaction mapper~~ (deleted/legacy)
- `src/exporter/` — Finance agent mapper
- `src/scheduler/` — Sync job orchestration
- `src/core/` — Config (pydantic-settings), logging
- `scripts/export_csv.py` — CLI for full financial CSV export (accounts, depot, overview)
- `main.py` — FastAPI app + APScheduler

## Key Patterns

- All config via pydantic-settings / `.env`
- Async httpx for all HTTP calls
- `uv` as package manager — use `uv run` to execute scripts
- APScheduler for periodic sync (planned)
- Dedup via external_id in normalization pipeline
- Conventional Commits required
- Docker: two-container architecture (export job + API), shared named volume

## Important Notes

- Comdirect auth requires TAN confirmation (pushTAN) — interactive step
- Comdirect transactions API does NOT support `paging-first > 0` — use single request with `paging-count=500`
- Depot transactions support `min-bookingDate` parameter (YYYY-MM-DD or -Xd offset)
- CSV format: semicolon-delimited, UTF-8-sig encoding, German number/date formats
- Run locally: `uv run uvicorn main:app --reload`
- Run export: `uv run python scripts/export_csv.py --output-dir exports`

---

## AI Agent Skill

The maintained repository skill is `.openclaw/skills/k-fin-finance-api/`.
It uses the configured k-fin MCP/Finance API and its current OpenAPI schema;
the old localhost CSV-export service is not the integration contract.

When an API change alters authentication, operation semantics, or the skill's
access workflow, update `SKILL.md` and the relevant parts of `references/api.md`.
Keep endpoint schemas in OpenAPI rather than duplicating an endpoint catalog in
the skill. Preserve read-only banking and the distinction between reading
financial data and authorizing mutations of derived records.

---

# Security and Data Privacy

**CRITICAL:** This project handles highly sensitive personal financial data, banking credentials, and API keys. Security is the absolute highest priority.

## 1. Zero Hardcoding of Secrets
- NEVER hardcode any secrets (PINs, passwords, client IDs, client secrets, access tokens, TANs).
- All secrets MUST be loaded via environment variables (pydantic-settings).
- `.env` and credential files must be in `.gitignore` and NEVER committed.

## 2. Read-Only Banking Access
- The Comdirect API integration MUST be strictly read-only.
- NEVER implement API calls that mutate bank state (transfers, settings changes).
- HTTP POST/PUT only for OAuth/Authentication flows.

## 3. No Sensitive Logging
- NEVER log account numbers, IBANs, balances, transaction details, or auth tokens.
- Mask any data if logging is required for debugging (e.g. `IBAN: DE** **** 1234`).

## 4. Data Transmission Boundaries
- Data flows from Comdirect into the local Postgres DB via the Normalization Pipeline.
- No telemetry, analytics, or third-party API calls that could leak financial data.

## 5. Safe Dependency Management
- Only use trusted, widely verified dependencies.
- No unnecessary packages that could pose supply chain risks.

## 6. AI Agent Constraints
- NEVER use real or realistic personal data in code, tests, or examples.
- Always use obvious dummy data (e.g. `DE00000000000000000000`, `John Doe`, `0.00`).
- Refuse and warn if asked to violate these constraints.