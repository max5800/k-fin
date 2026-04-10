# Kubernetes Deployment

This project can be deployed to Kubernetes, but the deployment model must respect one hard constraint:

**Comdirect requires manual pushTAN confirmation.**
There is no safe unattended read-only token flow.

## Deployment method

The canonical deployment method is the **Helm chart** in `chart/` (and **Tilt** for local development).

### Local development

```bash
tilt up --stream -- --profile=local
```

### Remote deployment

```bash
helm upgrade --install comdirect-sync ./chart -f dev/values.remote.yaml
```

## Architecture

### 1. Read-only API deployment

The API container serves already exported CSV files and does **not** need bank credentials.
This is the safe always-on component. The PVC is mounted read-only.

### 2. Manual export job

The export runner needs the Comdirect credentials and therefore must be treated as a manually triggered, security-sensitive workload.
Do **not** run it as an unattended CronJob unless the auth model changes.

The export job is the **only** workload that receives the Comdirect credentials via `envFrom`.

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

**The API deployment does NOT receive these secrets. Only the export job does.**

## Important

- The API should **not** get the finance credentials.
- The manual export job **does** get the finance credentials.
- If you later add Firefly import or agentic analysis, keep those concerns as separate workloads.
