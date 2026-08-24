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
| `src/mcp_server/` | MCP server exposing the Finance API as agent tools; read-only unless write tools are explicitly enabled. |
| `src/exporter/` | Finance agent mapper + model-based JSON export. |
| `src/scheduler/` | Sync job orchestration and backfill driver (worker-side). |
| `src/services/trustworthy_analytics.py` | Completeness-gated, versioned accounting and monthly-review facts. |
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

The deterministic monthly-review contract is documented in
[`trustworthy-analytics.md`](trustworthy-analytics.md). It is deliberately
separate from LLM interpretation and runs only after verified source periods.

## MCP Server

The MCP server in `src/mcp_server/` is a stdio adapter over the Finance API,
not a direct database client. On startup it fetches `/openapi.json`, converts
safe operations into MCP tools, and forwards calls back to the API with
`FINANCE_API_TOKEN`.

The default tool surface is read-only: only `GET` operations are registered.
Write tools are opt-in via `MCP_ENABLE_WRITE_TOOLS=true` and still pass through
an explicit allowlist in `src/mcp_server/openapi_tools.py`. At the moment the
only allowed write is budget upsert:

```text
PUT /api/v1/categories/budgets/{category_id}
```

This keeps OpenClaw portable and safe by default while still allowing a trusted
local session to adjust budgets through the same API path the UI uses. The E2E
guard in `tests/test_mcp_finance_api_e2e.py` verifies the intended chain:
OpenAPI -> MCP tool descriptor -> Finance API -> Postgres.
