# CLAUDE.md — k-fin

## Project

k-fin — Personal Finance Intelligence Platform. Read-only financial data export from the Comdirect REST API — as CSV, Finance API, and normalization pipeline into Postgres. This is a personal banking application handling highly sensitive financial data.

**Public OSS repo, single maintainer.** Source ships on GitHub under MIT — anything that lands on `main` is world-readable. Treat every commit as published: no real IBANs, balances, hostnames, tokens, or personal infra paths in code, fixtures, comments, configs, or chart defaults. Maintainer-only Helm values live in `dev/values.local.yaml` (git-ignored); `dev/values.remote.example.yaml` and `chart/values.yaml` are the public-facing templates.

## Identity

`k-fin` resolves internally to "Klaus Fin" (fish-fin). Klaus is the user's OpenClaw AI assistant — a talking goldfish, former GDR figure-skating champion, living on the home network. k-fin is his finance workbench. **Always write the product as `k-fin`** (lowercase, hyphenated) — never spell out "Klaus Finanzen" or "Klaus Finance" in code, UI, logs, commits, or docs. Keep banking code serious; identity stays subtle. Full lore: user's Obsidian vault at `Tech/Firefly & Finanz Sync/IDENTITY.md`.

## Commands

- `uv run python scripts/export_csv.py --output-dir exports` — Run CSV export
- `uv run python scripts/export_json.py --output-dir exports --pretty` — Run JSON export
- `uv run uvicorn main:app --reload` — Start dev server
- `uv run pytest` — Run tests
- `uv run ruff check .` — Lint
- `tilt up --stream` — Deploy the full stack to the k3s-app dev cluster

## Architecture

Two-microservice split with strict secret separation:
- **comdirect-api** (port 8000) — public-facing, read-only API, no bank secrets
- **comdirect-worker** (port 8001) — internal only, holds bank secrets, NetworkPolicy restricts access to api only

### Source Modules

- `src/external/` — Upstream provider clients (Comdirect REST + OAuth2/pushTAN, yfinance) — strictly read-only
- `src/api/` — FastAPI app, routers, JWT auth (`src/api/auth/`)
- `src/normalization/` — Ingest + canonicalize pipeline for Postgres
- `src/agents/` — LLM agents (categorization, anomaly, monthly analysis, orchestrator)
- `src/mcp_server/` — MCP server exposing the Finance API as agent tools
- `src/exporter/` — Finance agent mapper + model-based JSON export
- `src/scheduler/` — Sync job orchestration (comdirect-worker)
- `src/core/` — Config (pydantic-settings), logging, SQLAlchemy DB models
- `alembic/` — DB migrations

## Key Rules

- **Security first** — this handles banking credentials and financial data
- All secrets via environment variables, never hardcoded
- Comdirect access is strictly read-only — never implement write operations
- Never log sensitive data (IBANs, balances, tokens, PINs)
- Use obvious dummy data in tests (`DE00000000000000000000`, `John Doe`)
- **Secret scanning is mandatory.** `.husky/pre-commit` runs `gitleaks protect --staged` against [.gitleaks.toml](.gitleaks.toml); CI re-runs it. When introducing a new secret-shaped pattern (credential env var, IBAN-shaped fixture, personal hostname), either fix it or add a narrow allowlist entry in `.gitleaks.toml` with a comment explaining why it's safe. Never use `git commit --no-verify`. Required tool: `brew install gitleaks`.
- **Personal infra defaults stay local.** `dev/values.local.yaml` (git-ignored) holds the maintainer's real Helm overrides; `dev/values.remote.example.yaml` is the public template. The Tiltfile reads from `values.local.yaml`.
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
