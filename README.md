<p align="center">
  <img src="docs/assets/logo.png" alt="k-fin logo" width="300"/>
</p>

# 🏦 K-Fin (Personal Finance Intelligence Platform)

**Read-only financial data platform powered by the Comdirect REST API — CSV export, Finance API, and normalization pipeline.**

> A personal project, not a finished product. I run it daily on my own bank data and iterate from real usage — features land when I miss them, not on a public roadmap. It works well enough that I trust it with my own finances; whether that bar is high enough for you is your call. No guarantees, no support.

**Companion repo:** [k-fin-ui](https://github.com/max5800/k-fin-ui) — React frontend for this backend.

## What it does

Connects to the Comdirect REST API (read-only), normalizes your financial data into a local Postgres database, and serves it via a Finance API. Also supports CSV/JSON export for AI agents and dashboards.

- **Comdirect connector** — OAuth2 + pushTAN authentication, strictly read-only
- **CSV/JSON export** — Accounts, transactions, depot positions, depot transactions, financial overview
- **Finance API** — REST API for normalized financial data (transactions, categorization, aggregates)
- **Normalization pipeline** — Ingests raw Comdirect data into a canonical schema in Postgres
- **AI categorization** — LLM agents (pydantic-ai + Claude) categorize transactions, detect anomalies, generate monthly summaries
- **MCP server** — Exposes the read-only Finance API as MCP tools for agent use
- **Docker / Kubernetes** — Two-microservice architecture: public API + internal worker

## Architecture

The project is split into two microservices with strict secret separation:

| Service | Port | Role | Bank Secrets |
|---------|------|------|-------------|
| **comdirect-api** | 8000 | Public-facing read-only API | No |
| **comdirect-worker** | 8001 | Internal export/sync worker | Yes |

A Kubernetes NetworkPolicy ensures only `comdirect-api` can reach `comdirect-worker`. Both services share a PVC for exported data.

### Source Modules

| Module | Description |
|--------|-------------|
| `src/connector/` | Comdirect API client (auth, accounts, transactions, depot) |
| `src/api/` | FastAPI app, routers, JWT auth (`src/api/auth/`) |
| `src/normalization/` | Ingest + canonicalize pipeline for Postgres |
| `src/agents/` | LLM agents — categorization, anomaly detection, monthly analysis, orchestrator |
| `src/mcp_server/` | MCP server exposing the Finance API as agent tools |
| `src/exporter/` | Finance agent mapper + model-based JSON export |
| `src/scheduler/` | Sync job orchestration (comdirect-worker) |
| `src/core/` | Config (pydantic-settings), logging, SQLAlchemy DB models |
| `alembic/` | DB migrations |
| `scripts/` | Export, report, migration, and debug CLIs |

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.13 |
| Package manager | uv |
| HTTP client | httpx (async) |
| API | FastAPI + uvicorn |
| Config | pydantic-settings, .env |
| Containers | Docker, Helm, Tilt |
| Releases | semantic-release |

## Prerequisites

- Python 3.13+ and uv
- Postgres 16+ (local instance or Docker)
- Comdirect API credentials (Client ID, Client Secret, account number, PIN) — register at <https://developer.comdirect.de/>
- A pushTAN-capable device for authentication
- Docker (optional, for container setup)
- An Anthropic API key if you want the AI categorization features

## Setup

```bash
git clone https://github.com/max5800/k-fin.git
cd k-fin

# Python deps
uv sync

# Config — fill in your Comdirect creds, API_TOKEN, DATABASE_URL,
# OWN_IBANS, ANTHROPIC_API_KEY (see .env.example for the full list)
cp .env.example .env
$EDITOR .env

# Database — apply schema migrations (uses DATABASE_URL from .env)
uv run alembic upgrade head
```

## Usage

### Export to CSV

```bash
uv run python scripts/export_csv.py --output-dir exports
```

This triggers the Comdirect OAuth flow (including pushTAN confirmation) and writes CSV files to `exports/`.

### Start the REST API

```bash
uv run uvicorn main:app --reload
```

The API serves the exported CSVs at `http://localhost:8000`.

## Kubernetes

Deploy via the **Helm chart** in `chart/` and **Tilt** for dev:

```bash
# One-time: create your local values from the template
cp dev/values.remote.example.yaml dev/values.local.yaml
$EDITOR dev/values.local.yaml   # set ingress.host, CORS origins, Vault paths

# Remote dev stage — deploys to k3s-app cluster as k-fin-dev
tilt up --stream

# Direct Helm alternative
helm upgrade --install k-fin-dev ./chart -f dev/values.local.yaml
```

`dev/values.local.yaml` is git-ignored; only the `.example.yaml` template is checked in.

The remote dev stage deploys to whatever `ingress.host` you set in `dev/values.local.yaml` and exposes the FastAPI Swagger UI at `/docs`. Tilt links in the dashboard point directly to Swagger, ReDoc, and the health endpoint.

The chart deploys two microservices:

- **comdirect-api** (port 8000) — public-facing, read-only API. No bank credentials. Triggers syncs by calling the worker.
- **comdirect-worker** (port 8001) — internal only. Holds Comdirect credentials via ExternalSecret/Vault. A NetworkPolicy restricts ingress to `comdirect-api` only.

Both services share a PVC for exported data.

See `docs/kubernetes-deployment.md` for the full deployment guide.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /exports?token=...` | List all available CSV files |
| `GET /exports/latest?token=...` | Latest file per export category |
| `GET /exports/{filename}?token=...` | Download a specific CSV |

Set `API_TOKEN` in your `.env` to require authentication.

## Important Security & Architecture Note

- **Manual pushTAN is a feature, not a bug:** Because Comdirect does not offer scoped read-only API tokens, requiring a manual pushTAN confirmation for every sync is a deliberate security boundary. It ensures no automated system can quietly access your bank data or initiate sessions without your physical device approval.
- **Your credentials never leave your machine.** All data flows from Comdirect into your local Postgres DB via the normalization pipeline.
- **Read-only API.** The Comdirect connector implements only `GET` operations. Write operations (transfers, orders) are not and will not be supported.
- Never commit your `.env` file. It contains banking credentials.

## Disclaimer

This is a personal project shared as-is under the Apache 2.0 license (see [LICENSE](LICENSE)).

- **No warranty.** Use at your own risk. Read the full warranty disclaimer in `LICENSE`.
- **Banking data is sensitive.** You are responsible for securing your own deployment — credentials, network exposure, database access, backups, and the host running k-fin. Don't put this on the public internet without auth and TLS.
- **Not affiliated with Comdirect.** k-fin is an independent open-source project. It uses the public Comdirect REST API with end-user credentials. The Comdirect API specification (`docs/comdirect_REST_API_Dokumentation.*`) is not included in this repository — fetch it directly from <https://developer.comdirect.de/> for local reference.
- **No support guarantee.** Issues and PRs are welcome but answered as time allows.

For security-relevant findings, see [SECURITY.md](SECURITY.md).

## Built with AI

I do not write the code in this project. Claude (via OpenClaw) writes it; I direct the architecture, security model, and product decisions, and review every change before it ships. Treat this as a human-curated AI-built codebase rather than a hand-written one — the design choices and the trust boundary are mine, the implementation is the model's.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
