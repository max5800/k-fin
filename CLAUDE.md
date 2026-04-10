# CLAUDE.md — comdirect-firefly-sync

## Project

Read-only financial data export from the Comdirect REST API — as CSV, REST API, or (planned) Firefly III import. This is a personal banking application handling highly sensitive financial data.

## Commands

- `uv run python scripts/export_csv.py --output-dir exports` — Run CSV export
- `uv run python scripts/export_json.py --output-dir exports --pretty` — Run JSON export
- `uv run uvicorn main:app --reload` — Start dev server
- `uv run pytest` — Run tests
- `uv run ruff check .` — Lint
- `docker-compose up` — Start both containers

## Architecture

Two-microservice split with strict secret separation:
- **comdirect-api** (port 8000) — public-facing, read-only API, no bank secrets
- **comdirect-worker** (port 8001) — internal only, holds bank secrets, NetworkPolicy restricts access to api only

### Source Modules

- `src/connector/` — Comdirect API client (OAuth2 + pushTAN, strictly read-only)
- `src/api/` — Read-only FastAPI serving exported CSVs (comdirect-api)
- `src/importer/` — Firefly III client + transaction mapper (planned)
- `src/exporter/` — Finance agent mapper + model-based JSON export
- `src/scheduler/` — Sync job orchestration (comdirect-worker)
- `src/core/` — Config (pydantic-settings), logging

## Key Rules

- **Security first** — this handles banking credentials and financial data
- All secrets via environment variables, never hardcoded
- Comdirect access is strictly read-only — never implement write operations
- Never log sensitive data (IBANs, balances, tokens, PINs)
- Use obvious dummy data in tests (DE00000000000000000000, John Doe)
- Conventional commits required
- Python 3.13, async httpx, FastAPI, pydantic-settings, uv

## Agent Team

This project uses specialized review agents in `.claude/agents/`:
- `security-reviewer` — AppSec engineer for banking security
- `platform-reviewer` — DevOps for Docker/CI/CD
- `code-reviewer` — Senior Python dev for quality/tests
- `architect` — Architecture and design review

Run `/full-review` for a complete team review, `/security-check` for security-only.
