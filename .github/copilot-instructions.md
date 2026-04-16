# Copilot Instructions — K-Fin

## Project

K-Fin — Personal Finance Intelligence Platform. Read-only financial data export from the Comdirect REST API — as CSV, Finance API, and normalization pipeline into Postgres.

## Architecture

- `src/connector/` — Comdirect API client (OAuth2 + pushTAN auth, accounts, transactions, depot)
- `src/api/` — Read-only FastAPI to serve exported CSVs (runs in isolated container)
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

## AI Agent Skill — REQUIRED

This project exposes a **read-only Finance API** (`src/api/serve_exports.py`) used by AI agents (Klaus/OpenClaw) to query financial data.

**The OpenClaw skill lives at:** `.openclaw/skills/comdirect-finance-api/` (in this repository)

### Rules: always update the skill when changing the API

1. **After ANY change to `src/api/serve_exports.py`** — update the skill:
   - New endpoint → add to endpoints table in `SKILL.md`
   - Auth change → update connection section in `SKILL.md`
   - New CSV format → update `references/api.md`

2. **After adding new export types** (new filename prefix) → add to export categories table in `SKILL.md`

3. **After changing CSV format** (columns, delimiter, encoding) → update `references/api.md`

4. **The skill must always reflect the actual API** — a stale skill causes the agent to call wrong endpoints or misparse data

### Skill location in this repository

```
.openclaw/skills/comdirect-finance-api/
├── SKILL.md               # Main skill: endpoints, auth, workflow
└── references/
    └── api.md             # Response format examples, CSV parsing
```

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