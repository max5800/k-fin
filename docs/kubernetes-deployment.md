# Kubernetes Deployment

This project can be deployed to Kubernetes, but the deployment model must respect one hard constraint:

**Comdirect requires manual pushTAN confirmation.**
There is no safe unattended read-only token flow.

## Recommended split

### 1. Read-only API deployment
The API container serves already exported CSV files and does **not** need bank credentials.
This is the safe always-on component.

### 2. Manual export job
The export runner needs the Comdirect credentials and therefore must be treated as a manually triggered, security-sensitive workload.
Do **not** run it as an unattended CronJob unless the auth model changes.

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

In Max' homelab, these are projected into Kubernetes via a namespace-scoped `SecretStore` and `ExternalSecret`.

## Files in `k8s/`

- `api-deployment.yaml` — always-on read-only API
- `api-service.yaml` — ClusterIP service for the API
- `manual-export-job.yaml` — manual export job template using `comdirect-secrets`

## Important

- The API should **not** get the finance credentials.
- The manual export job **does** get the finance credentials.
- If you later add Firefly import or agentic analysis, keep those concerns as separate workloads.
