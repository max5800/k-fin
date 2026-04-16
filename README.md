<p align="center">
  <img src="docs/assets/logo.png" alt="k-fin logo" width="300"/>
</p>

# 🏦 comdirect-firefly-sync

**Read-only financial data export from the Comdirect REST API — as CSV, REST API, or (planned) Firefly III import.**

> I built this for myself. It works for me. If it is useful to you, great — but this comes with no guarantees and no support.

## What it does

Connects to the Comdirect REST API (read-only), exports your financial data as CSV files, and serves them via a lightweight HTTP API. Planned: periodic import into Firefly III with deduplication.

- **Comdirect connector** — OAuth2 + pushTAN authentication, strictly read-only
- **CSV export** — Accounts, transactions, depot positions, depot transactions, financial overview
- **REST API** — Serves exported CSVs over HTTP (e.g. for AI agents or dashboards)
- **Docker / Kubernetes** — Two-microservice architecture: public API + internal worker
- **Firefly III import** — Planned: periodic sync with deduplication

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
| `src/api/` | Read-only FastAPI serving CSV exports (comdirect-api) |
| `src/importer/` | Firefly III client + transaction mapper *(planned)* |
| `src/scheduler/` | Sync job orchestration (comdirect-worker) |
| `src/core/` | Config (pydantic-settings), logging |
| `scripts/` | Export script, auth test, debug tools |

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.13 |
| Package manager | uv |
| HTTP client | httpx (async) |
| API | FastAPI + uvicorn |
| Config | pydantic-settings, .env |
| Containers | Docker, docker-compose |
| Releases | semantic-release |

## Prerequisites

- Python 3.13+ and uv
- Comdirect API credentials (Client ID, Client Secret, account number, PIN)
- A pushTAN-capable device for authentication
- Docker (optional, for container setup)

## Setup

```bash
git clone https://github.com/max5800/comdirect-firefly-sync.git
cd comdirect-firefly-sync
cp .env.example .env
# Fill in your credentials in .env
uv sync
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

### Docker

```bash
docker-compose up
```

## Kubernetes

Deploy via the **Helm chart** in `chart/` (or **Tilt** for local development):

```bash
# Local dev
tilt up --stream -- --profile=local

# Remote
helm upgrade --install comdirect-sync ./chart -f dev/values.remote.yaml
```

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
- **Your credentials never leave your machine.** All data flows between Comdirect and your local setup only.
- Never commit your `.env` file. It contains banking credentials.

## Built with AI

This project was built with AI-assisted development (primarily Claude via OpenClaw). Architecture, security rules, and code were collaboratively developed — but every merge went through a human review. AI writes code; humans decide what ships.

## License

MIT