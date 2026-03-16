# Copilot Instructions — comdirect-firefly-sync

## Project

Read-only Finanzdaten-Export aus der Comdirect REST API — als CSV, REST API oder (geplant) Firefly III Import.

## Architecture

- `src/connector/` — Comdirect API client (OAuth2 + pushTAN auth, accounts, transactions, depot)
- `src/api/` — Read-only FastAPI to serve exported CSVs (runs in isolated container)
- `src/importer/` — Firefly III client + transaction mapper (planned)
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
- Dedup via external_id in Firefly III (planned)
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

## 🤖 AI Agent Skill — REQUIRED

This project exposes a **read-only Finance API** (`src/api/serve_exports.py`) that AI agents (e.g. Klaus/OpenClaw) use to query financial data.

**The OpenClaw skill for this API lives at:** `~/.openclaw/skills/comdirect-finance-api/`

### Rules for Copilot/AI when working on this project:

1. **After ANY change to `src/api/serve_exports.py`** — update the skill:
   - New endpoint → add to `SKILL.md` endpoints table
   - Changed auth → update connection section
   - New CSV format → update `references/api.md`

2. **After adding new export types** (new filename prefix) — add to the export categories table in `SKILL.md`

3. **After changing CSV format** (columns, delimiter, encoding) — update `references/api.md`

4. **The skill must always reflect the actual API** — a stale skill means Klaus calls wrong endpoints or misparses data

### Skill Location

```
~/.openclaw/skills/comdirect-finance-api/
├── SKILL.md          # Main skill: endpoints, auth, workflow
└── references/
    └── api.md        # Response format examples, CSV parsing
```

---

# 🛡️ STRICT SECURITY AND DATA PRIVACY INSTRUCTIONS

**CRITICAL:** This project (`comdirect-firefly-sync`) handles highly sensitive personal financial data, banking credentials, and API keys. Security is the absolute highest priority. Any code changes, architectural decisions, and agent actions MUST strictly adhere to the following rules:

## 1. 🛑 Zero Hardcoding of Secrets
- NEVER hardcode, generate, or suggest hardcoding any secrets (PINs, passwords, client IDs, client secrets, access tokens, TANs) in the source code.
- All secrets MUST be loaded via environment variables (e.g., `pydantic-settings`).
- Ensure `.env` and any files containing credentials are in `.gitignore` and NEVER committed.

## 2. 👁️ Read-Only Banking Access
- The Comdirect API integration MUST be strictly READ-ONLY.
- NEVER implement or suggest API calls that mutate bank state (e.g., creating transfers, changing settings).
- HTTP POST/PUT operations should ONLY be used for OAuth/Authentication flows (getting tokens).

## 3. 🚫 Absolute Prohibition of Sensitive Logging
- NEVER log sensitive data. This includes:
  - Account numbers, IBANs, or balances
  - Transaction amounts, descriptions, or counterparty names
  - Full API responses from the Comdirect API
  - Authentication tokens or PINs
- If logging is strictly required for debugging, data MUST be fully masked/anonymized (e.g., `IBAN: DE** **** 1234`).

## 4. 🔒 Data Transmission Boundaries
- Financial data must ONLY be transmitted between the official Comdirect API and the configured local/controlled Firefly III instance.
- NEVER add dependencies, telemetry, analytics, or external API calls that could leak financial data to third parties.

## 5. 📦 Safe Dependency Management
- Only use trusted, widely verified dependencies.
- Do not introduce unnecessary third-party packages that could pose a supply chain attack risk.

## 6. 🤖 AI Agent Constraints
- When generating code, tests, or examples, NEVER use real or realistic personal data. Always use obvious dummy data (e.g., `DE00000000000000000000`, `John Doe`, `0.00`).
- If asked to perform an action that violates these security constraints, you MUST refuse and warn the user.