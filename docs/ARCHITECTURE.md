# k-fin Architecture

A high-level map of how k-fin is put together. For setup steps see
[`README.md`](../README.md); for project conventions see [`CLAUDE.md`](../CLAUDE.md).

## Two-microservice split

k-fin runs as two cooperating services with **strict secret separation**. Bank
credentials never touch the public-facing process.

| Service | Port | Role | Holds bank secrets |
|---------|------|------|--------------------|
| `comdirect-api` | 8000 | Public-facing read-only API; serves UI and MCP clients | No |
| `comdirect-worker` | 8001 | Internal sync/export worker; talks to Comdirect | Yes |

A Kubernetes `NetworkPolicy` allows traffic to `comdirect-worker` only from
`comdirect-api`. Both services share Postgres and a PVC for export artifacts.

## Source modules

One-line overviews. The code is the source of truth.

| Path | Responsibility |
|------|----------------|
| `src/external/` | Upstream provider clients — Comdirect REST (OAuth2 + pushTAN, accounts, transactions, depot) and yfinance (price history). Read-only. |
| `src/api/` | FastAPI app, routers, JWT auth (`src/api/auth/`), schemas, dependency wiring. |
| `src/normalization/` | Ingest + canonicalize raw Comdirect payloads into the canonical Postgres schema. |
| `src/agents/` | LLM agents — categorization, anomaly detection, monthly/weekly analysis, orchestrator, synthesizer. |
| `src/mcp_server/` | MCP server exposing the read-only Finance API as agent tools. |
| `src/exporter/` | Finance agent mapper + model-based JSON export. |
| `src/scheduler/` | Sync job orchestration and backfill driver (worker-side). |
| `src/core/` | Config (pydantic-settings), logging, SQLAlchemy DB models. |
| `alembic/` | DB migrations; runs on worker startup or via `scripts/migrate.py`. |
| `scripts/` | Export, report, migration, and debug CLIs. |

## Data flow

```
Comdirect REST API
        │  (OAuth2 + pushTAN, read-only)
        ▼
comdirect-worker (src/external → src/normalization)
        │  writes canonical rows
        ▼
   Postgres (canonical schema, managed by Alembic)
        │
        ▼
comdirect-api  (src/api, src/exporter, src/mcp_server)
        │  REST + JWT
        ▼
   k-fin-ui (React) / MCP clients / agents
```

Sync is **TAN-in-the-loop by design**: every Comdirect read requires interactive
pushTAN confirmation. There is no autonomous background sync.

## Secret handling

- **Production (K3s app cluster).** Vault holds the source of truth. ExternalSecrets
  Operator (ESO) materializes Kubernetes `Secret` objects from Vault paths; pods
  consume them as env vars. See `chart/templates/externalsecret.yaml` and
  [`docs/kubernetes-deployment.md`](kubernetes-deployment.md).
- **Local development.** Values live in `.env` (git-ignored), seeded from
  `.env.example`. Helm overrides for the dev cluster live in `dev/values.local.yaml`
  (git-ignored); the public template is `dev/values.remote.example.yaml`.
- **Never** put real secrets in `chart/values.yaml`, the Dockerfiles, or any
  committed `.example.yaml`. The pre-commit hook (`gitleaks`, see `.gitleaks.toml`)
  enforces this on every commit and CI re-runs it on every PR.
- The api service has **no** bank-secret env refs. The split is an architectural
  invariant, not a convention.

## DB Schema

_TODO_: schema diagram once Alembic stabilizes. Until then, treat the SQLAlchemy
models in `src/core/db/` and the migrations in `alembic/versions/` as the
reference.

## Agent Pipeline

_TODO_: describe orchestrator → categorization → anomaly → monthly/weekly →
synthesizer flow, including where runs are persisted and how rerun/cancel work.
Models and prompts live under `src/agents/`.

## MCP Server

_TODO_: document the tool surface, the explicit write allowlist (currently only
budget upsert), and how the MCP server reads from the Finance API rather than
the DB directly. Code: `src/mcp_server/`.
