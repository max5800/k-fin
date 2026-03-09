# CLAUDE.md — Agent Instructions

## Project
Read-only sync from Comdirect REST API to Firefly III.

## Architecture
- `src/connector/` — Comdirect API client (auth, accounts, transactions)
- `src/importer/` — Firefly III client + transaction mapper
- `src/scheduler/` — sync job orchestration
- `src/core/` — config, logging
- `main.py` — FastAPI app + APScheduler

## Key Patterns
- All config via pydantic-settings / .env
- Async httpx for all HTTP calls
- APScheduler for periodic sync
- Dedup via external_id in Firefly III
- Conventional Commits required

## Important Notes
- Comdirect auth requires TAN confirmation (photoTAN/pushTAN) — MVP does Step 1 only
- Never write to Comdirect — read-only only
- Never commit .env or credentials
- Run: `uv run uvicorn main:app --reload`
