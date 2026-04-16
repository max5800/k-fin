# CLAUDE.md — K-Fin

## Project

K-Fin — Personal Finance Intelligence Platform. Read-only financial data export from the Comdirect REST API — as CSV, Finance API, and normalization pipeline into Postgres. This is a personal banking application handling highly sensitive financial data.

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
- `src/importer/` — ~~Firefly III client + transaction mapper~~ (deleted/legacy)
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

Specialized agents live in `.claude/agents/`. The main Claude spawns them via the Task tool.

### Reviewers (read-only analysis)

- `security-reviewer` — AppSec for banking security
- `platform-reviewer` — DevOps for Docker/CI/CD
- `code-reviewer` — Senior Python dev for quality/tests
- `architect` — Architecture and design review

### Executors (can also write/run)

- `test-engineer` — writes/runs tests, knows the Comdirect API flow and FastAPI entry points
- `deployment-engineer` — Helm chart, K3s (app vs. infra cluster), Vault/ESO, Docker, release

### When to spawn which

- Adding a feature → `test-engineer` for coverage once code is in place
- Touching `chart/`, `Dockerfile`, `values*.yaml`, or `.env.example` → `deployment-engineer`
- Full audit → `/full-review` (spawns the 4 reviewers in parallel)
- Security-only audit → `/security-check`

Executors are invoked ad-hoc (not part of `/full-review`) because they make changes.
