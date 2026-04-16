# Firefly III Consumer Reframe

> **Status:** Proposal (2026-04-10)
> **Goal:** Reposition Firefly III as a downstream consumer, not the centre of the system.

## 1. Problem Statement

The project name ("comdirect-firefly-sync") and several internal labels frame
Firefly III as the system's destination.  In practice, Firefly is **one of
several** export targets (CSV, JSON/Finance Agent, REST API, and eventually
Firefly).  Treating it as the core creates three problems:

1. **Coupling in orchestration** -- `src/scheduler/sync_job.py` hard-wires
   the pipeline as "authenticate Comdirect -> push to Firefly".  Adding a
   new consumer means editing the sync job.
2. **Misleading entry point** -- `main.py` schedules a "Comdirect -> Firefly III
   sync" and exposes `/sync/trigger`, even though the Firefly importer is
   still incomplete and blocked by interactive TAN.
3. **Naming gravity** -- config keys (`firefly_*`), job names, and repo title
   pull contributors toward a Firefly-centric mental model, making it harder
   to reason about the system as a general finance-data platform.

## 2. Target Architecture

```
                          +-----------+
                          | Comdirect |
                          |  REST API |
                          +-----+-----+
                                |
                          authenticate + fetch
                                |
                        +-------v--------+
                        |   connector/   |   <-- core: read-only API client
                        |  (auth, fetch) |
                        +-------+--------+
                                |
                     normalised ComdirectData
                                |
                  +-------------+-------------+
                  |             |             |
           +------v---+  +-----v-----+  +----v------+
           | exporter/ |  | exporter/ |  | consumer/ |   <-- pluggable outputs
           |   CSV     |  |   JSON    |  | Firefly   |
           +-----------+  +-----------+  +-----------+
                                              |
                                         Firefly III
                                          (optional)
```

### Layer responsibilities

| Layer | Module(s) | Owns |
|-------|-----------|------|
| **Connector** | `src/connector/` | Auth flow, raw Comdirect API calls, `ComdirectData` model |
| **Export / Transform** | `src/exporter/`, `scripts/export_csv.py`, `scripts/export_finance_data.py` | Normalise `ComdirectData` into output formats (CSV, JSON, future Parquet, etc.) |
| **Serve** | `src/api/` | Read-only HTTP access to exported artefacts |
| **Consumer adapters** | `src/consumers/firefly/` (renamed from `src/importer/`) | Map normalised data into a specific downstream system's API; each adapter is self-contained |
| **Orchestration** | `src/scheduler/` | Trigger fetch-then-fan-out; does **not** know consumer internals |

### Key boundary rule

> The connector and export layers **never import from** a consumer adapter.
> Consumer adapters depend on `connector.models` (or the normalised export
> output) and nothing else.

## 3. What Can Be Kept As-Is

| Component | Status | Notes |
|-----------|--------|-------|
| `src/connector/` | **Keep** | Already clean; no Firefly knowledge |
| `src/connector/models.py` | **Keep** | `ComdirectData` is the canonical intermediate model |
| `src/exporter/finance_agent_mapper.py` | **Keep** | Good example of a pure transform |
| `src/api/app.py` | **Keep** | K-Fin Finance API — replaces legacy serve_exports.py |
| `scripts/export_csv.py` | **Keep** | Standalone, well-tested |
| `scripts/export_finance_data.py` | **Keep** | Standalone |
| `src/importer/firefly_client.py` | **Keep (move)** | Good HTTP client, just needs a new home |
| `src/importer/transaction_mapper.py` | **Keep (move)** | Clean mapper; tests pass |
| `src/core/config.py` | **Keep (minor edit)** | Group Firefly settings under an optional consumer block |
| `tests/test_transaction_mapper.py` | **Keep** | 12 solid test cases |

## 4. What Should Change

Changes are ordered from least to most invasive.  Items 1-3 are
documentation/naming only; items 4-6 are small, low-risk code moves.

### 4.1 Rename the conceptual frame (docs + config only)

- **Scheduler job name** in `main.py`: change from
  `"Comdirect -> Firefly III sync"` to `"Comdirect data sync"`.
- **FastAPI metadata** in `main.py`: update `title` and `description` to
  reflect "finance data platform", not "sync to Firefly".
- **README / CLAUDE.md**: replace Firefly-centric language with
  "consumers" or "downstream adapters".

### 4.2 Move `src/importer/` -> `src/consumers/firefly/`

Current:
```
src/importer/
  firefly_client.py
  transaction_mapper.py
```

Proposed:
```
src/consumers/
  __init__.py
  firefly/
    __init__.py
    client.py              # was firefly_client.py
    transaction_mapper.py  # unchanged
```

This makes room for future consumer adapters (e.g.
`src/consumers/ynab/`, `src/consumers/actual_budget/`) without polluting
a single `importer` namespace.  Internal imports in `sync_job.py` and
tests update accordingly.

### 4.3 Decouple the scheduler from Firefly

`sync_job.py` currently hard-wires the entire pipeline.  Refactor into:

```python
# src/scheduler/sync_job.py

async def run_sync(consumers: list[Callable] | None = None):
    """Fetch data, then fan out to registered consumers."""
    comdirect = ComdirectClient()
    auth_ok = await comdirect.authenticate_full()
    if not auth_ok:
        return

    data: ComdirectData = await comdirect.get_all_data()

    for consume in (consumers or _default_consumers()):
        try:
            await consume(data)
        except Exception:
            logger.exception("Consumer failed")
```

Each consumer adapter exposes an `async def consume(data: ComdirectData)` entry
point.  The Firefly consumer becomes one such function:

```python
# src/consumers/firefly/consumer.py

async def consume(data: ComdirectData) -> None:
    client = FireflyClient()
    # ... map + push, same logic as current sync_job lines 50-87
```

This keeps the scheduler generic and lets us add CSV-refresh, JSON-export,
or webhook-notify consumers without touching `sync_job.py`.

### 4.4 Make Firefly config optional

In `src/core/config.py`, the `firefly_base_url` and
`firefly_access_token` fields currently have defaults that silently
allow the app to start without Firefly.  Make the intent explicit:

```python
# Optional — only needed when the Firefly consumer is enabled
firefly_base_url: str = ""
firefly_access_token: str = ""
firefly_enabled: bool = False
```

The Firefly consumer checks `firefly_enabled` at registration time and
skips itself if not configured.  This avoids confusing "No Firefly
account found" warnings when Firefly was never intended.

### 4.5 Generalise the `/sync/trigger` endpoint

Today the endpoint is Firefly-specific.  After the refactor it triggers
the generic `run_sync()`, which fans out to all enabled consumers.  No
API change needed; the semantics simply broaden.

### 4.6 (Future) Consumer registration via entry points or config list

For now, a simple list in `sync_job.py` is sufficient:

```python
def _default_consumers():
    consumers = []
    if settings.firefly_enabled:
        from src.consumers.firefly.consumer import consume as firefly_consume
        consumers.append(firefly_consume)
    return consumers
```

A plugin/entry-point system is overkill at this stage but becomes natural
if a third consumer is ever added.

## 5. Migration Path

| Step | Risk | Effort |
|------|------|--------|
| Update docs, job names, FastAPI metadata | None | ~30 min |
| Move `src/importer/` -> `src/consumers/firefly/` | Low (import path changes) | ~1 hr |
| Refactor `sync_job.py` to fan-out pattern | Low (existing tests cover mapper) | ~1 hr |
| Add `firefly_enabled` config flag | None | ~15 min |
| Update tests (import paths) | Low | ~30 min |
| Rename repo (optional, coordinate with CI) | Medium (external references) | Separate PR |

Total: roughly half a day of focused work, all behind the existing
feature flag (Firefly is already non-functional without valid creds).

## 6. What This Does NOT Change

- **Connector internals** -- auth flow, data models, API calls stay identical.
- **CSV / JSON export** -- these are already decoupled; no change needed.
- **API container** -- still serves from `/data/exports`, still credential-free.
- **Security model** -- no new attack surface; consumer adapters inherit the
  same "read-only, no secret logging" rules.
- **Docker topology** -- export + api containers remain; the consumer runs
  inside the export container (same trust boundary).

## 7. Open Questions

1. **Repo rename?**  `comdirect-firefly-sync` -> `comdirect-finance-sync` or
   `comdirect-data-platform`.  Nice to have but creates downstream churn
   (CI, K8s manifests, docs links).  Can be a follow-up.
2. **Should consumers write to the export volume?**  If Firefly import
   results (success/failure counts) are useful for the API layer, we could
   write a small JSON manifest.  Low priority.
3. **Consumer-specific scheduling?**  Some consumers might want different
   intervals.  For now, a single fan-out interval is fine; split later if
   needed.
