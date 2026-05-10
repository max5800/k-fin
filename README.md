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
- **Self-hostable** — `docker compose up` for a single laptop or a Helm chart for K3s/K8s; two-microservice split keeps bank credentials off the public-facing API

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
| `src/external/` | Upstream provider clients — Comdirect (auth, accounts, transactions, depot) and yfinance (price history) |
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

### One-off ops scripts

#### Refund-audit backfill report

After upgrading past `v1.33.0` (refund-aware accounting), run this once
to list historical positive-amount rows in the `erstattungen` bucket so
you can decide which are real income (Steuerrückzahlung, Cashback) and
which are misclassified refunds of a prior expense (Krankenkasse,
Splitwise, Retouren). The script is **read-only**.

```bash
# Local
uv run python scripts/refund_backfill_report.py
uv run python scripts/refund_backfill_report.py --json

# In-cluster (worker pod has the venv + DATABASE_URL ready)
kubectl exec deploy/k-fin-worker -- \
    /app/.venv/bin/python scripts/refund_backfill_report.py
```

Most rows are auto-decided by `apply_refund_heuristic` on API startup
and via `POST /api/v1/aggregates/refund-audit/auto-apply`. Use the
script when you want a printable list before doing manual `PATCH`es via
`/api/v1/transactions/{id}`.

## Deployment

Three supported paths, ordered by setup effort:

### 1. Docker Compose (no Kubernetes required)

The fastest way to run the full stack — api, worker, UI, and Postgres —
on a laptop or a single VM. No K8s, no Helm, no operators.

```bash
cp .env.compose.example .env
$EDITOR .env       # fill in COMDIRECT_*, rotate the change_me_* values
docker compose pull && docker compose up -d
open http://localhost:3000
```

The compose file pulls prebuilt images from
`ghcr.io/max5800/k-fin-{api,worker,ui}` by default. Worker port 8001 is
intentionally not published — only the api can reach it on the
compose-internal network, mirroring the chart's NetworkPolicy. See the
top of [`docker-compose.yml`](docker-compose.yml) for the full layout.

> **pushTAN required.** Every sync needs interactive Comdirect pushTAN
> approval on your phone — there is no fully-headless mode. This is a
> deliberate security boundary, not a missing feature.

### 2. K3s / Kubernetes via Helm

If you already run a cluster, install via the Helm chart in [`chart/`](chart/)
or directly from the published OCI chart:

```bash
helm upgrade --install k-fin oci://ghcr.io/max5800/helm-charts/k-fin \
  --version 1.28.2 -n k-fin --create-namespace -f my-values.yaml
```

The chart deploys api, worker, UI, a CloudNativePG `Cluster` for Postgres,
a NetworkPolicy isolating the worker, and a `pre-install,pre-upgrade`
migration job. K3s with Traefik + CloudNativePG is the maintainer's
tested reference platform; plain K8s works with `ingress.className`
overrides.

The full guide — prerequisites, BYO-Secret path, ExternalSecrets path,
backups, upgrades — is in [`docs/kubernetes-deployment.md`](docs/kubernetes-deployment.md).

### 3. Local development (maintainer workflow)

The maintainer iterates against a remote K3s cluster with Tilt, Rancher
Desktop, and ExternalSecrets pulling from Vault. **You do not need this
path to install or run k-fin** — it's specific to the maintainer's
homelab. If you want to contribute, see [`docs/local-development.md`](docs/local-development.md)
for the full setup.

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
