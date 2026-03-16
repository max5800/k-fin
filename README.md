# 🏦 comdirect-firefly-sync

**Read-only financial data export from the Comdirect REST API — as CSV, REST API, or (planned) Firefly III import.**

> I built this for myself. It works for me. If it is useful to you, great — but this comes with no guarantees and no support.

## What it does

Connects to the Comdirect REST API (read-only), exports your financial data as CSV files, and serves them via a lightweight HTTP API. Planned: periodic import into Firefly III with deduplication.

- **Comdirect connector** — OAuth2 + pushTAN authentication, strictly read-only
- **CSV export** — Accounts, transactions, depot positions, depot transactions, financial overview
- **REST API** — Serves exported CSVs over HTTP (e.g. for AI agents or dashboards)
- **Docker** — Two-container setup: export job + API server with shared volume
- **Firefly III import** — Planned: periodic sync with deduplication

## Architecture

| Module | Description |
|--------|-------------|
| `src/connector/` | Comdirect API client (auth, accounts, transactions, depot) |
| `src/api/` | Read-only FastAPI serving CSV exports |
| `src/importer/` | Firefly III client + transaction mapper *(planned)* |
| `src/scheduler/` | Sync job orchestration |
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

The API serves the exported CSVs at `http://localhost:8001`.

### Docker

```bash
docker-compose up
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /exports?token=...` | List all available CSV files |
| `GET /exports/latest?token=...` | Latest file per export category |
| `GET /exports/{filename}?token=...` | Download a specific CSV |

Set `API_TOKEN` in your `.env` to require authentication.

## Important

- **This is personal software.** I built it for my own use. No warranties, no support.
- **Your credentials never leave your machine.** All data flows between Comdirect and your local setup only.
- Never commit your `.env` file. It contains banking credentials.
- Comdirect auth requires manual pushTAN confirmation each time.

## Built with AI

This project was built with AI-assisted development (primarily Claude via OpenClaw). Architecture, security rules, and code were collaboratively developed — but every merge went through a human review. AI writes code; humans decide what ships.

## License

MIT