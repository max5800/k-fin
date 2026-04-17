# Kubernetes Deployment

This project can be deployed to Kubernetes, but the deployment model must respect one hard constraint:

**Comdirect requires manual pushTAN confirmation.**
There is no safe unattended read-only token flow.

## Deployment method

The canonical deployment method is the **Helm chart** in `chart/` with **Tilt** for iterative dev.

### Dev stage (remote k3s-app cluster)

```bash
tilt up --stream
```

### Direct Helm (without Tilt)

```bash
helm upgrade --install k-fin-dev ./chart -f dev/values.remote.yaml
```

### Accessing Swagger UI

After deploying the dev stage, the FastAPI Swagger UI is available at:

```
https://k-fin-dev.max5800.com/docs
```

The OpenAPI JSON schema is at `/openapi.json`. No additional Ingress path
configuration is needed — FastAPI serves `/docs` by default on the API port.

## Architecture — Two-Service Model

The deployment consists of two microservices with strict secret separation:

### 1. comdirect-api (Public)

- **Port:** 8000
- **Role:** Public-facing, read-only API serving exported CSVs. Also triggers sync by calling the worker.
- **Secrets:** None — no bank credentials
- **PVC:** Shared volume mounted read-only for serving exports

### 2. comdirect-worker (Internal)

- **Port:** 8001
- **Role:** Internal worker that performs Comdirect data export. Requires manual pushTAN confirmation.
- **Secrets:** Receives Comdirect credentials via `envFrom` (ExternalSecret/Vault)
- **PVC:** Shared volume mounted read-write for writing exports

Do **not** run the worker as an unattended CronJob unless the auth model changes.

### NetworkPolicy

A NetworkPolicy restricts ingress to `comdirect-worker` so that only `comdirect-api` can reach it. No other pod in the cluster can call the worker directly. This enforces the secret boundary — the API acts as the only gateway to the worker.

### PVC Sharing

Both services mount the same PersistentVolumeClaim:
- `comdirect-worker` writes exports to the PVC
- `comdirect-api` reads and serves them from the same PVC

## Secrets via Vault + ESO

Recommended Vault path:

```text
secret/finance/comdirect
```

Expected fields:

- `COMDIRECT_CLIENT_ID`
- `COMDIRECT_CLIENT_SECRET`
- `COMDIRECT_USERNAME`
- `COMDIRECT_PIN`

These are projected into Kubernetes via ExternalSecret (`chart/templates/externalsecret.yaml`).
Enable with `externalSecret.enabled: true` in your values file.

**Only `comdirect-worker` receives these secrets. The API deployment does NOT.**

## Important

- `comdirect-api` must **not** receive bank credentials.
- `comdirect-worker` is the only workload with bank credentials and must not be publicly accessible.
- The NetworkPolicy ensures only `comdirect-api` can call `comdirect-worker`.
- If you later add Firefly import or agentic analysis, keep those concerns as separate workloads.
