# Kubernetes Deployment Guide

This guide installs k-fin into a Kubernetes cluster via the bundled Helm
chart in [`chart/`](../chart). It assumes you have `kubectl` and `helm`
configured against the cluster you want to deploy to.

> Looking for something simpler? The [Docker Compose stack](../README.md#run-with-docker-compose)
> in the repo root runs k-fin without Kubernetes — same topology, single
> command. Use that if you don't already operate a cluster.

## TL;DR

Once the prerequisites below are met:

```bash
kubectl create namespace k-fin

# Create the two Secrets the chart expects (replace <release> with your
# helm release name, e.g. "k-fin"):
kubectl -n k-fin create secret generic <release>-k-fin-comdirect-secrets \
  --from-literal=COMDIRECT_CLIENT_ID=... \
  --from-literal=COMDIRECT_CLIENT_SECRET=... \
  --from-literal=COMDIRECT_USERNAME=... \
  --from-literal=COMDIRECT_PIN=...

kubectl -n k-fin create secret generic <release>-k-fin-infra-secrets \
  --from-literal=API_TOKEN=$(openssl rand -hex 32) \
  --from-literal=JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=BOOTSTRAP_USER_EMAIL=you@example.com \
  --from-literal=BOOTSTRAP_USER_INITIAL_PASSWORD=$(openssl rand -base64 18)

# Install from the chart in this repo …
helm upgrade --install <release> ./chart -n k-fin -f my-values.yaml

# … or from the published OCI chart (no clone needed):
helm upgrade --install <release> oci://ghcr.io/max5800/helm-charts/k-fin \
  --version 1.28.2 -n k-fin -f my-values.yaml
```

The migration job runs automatically as a Helm `pre-install,pre-upgrade`
hook — no manual schema setup needed.

## Tested platforms

The reference platform the maintainer runs on is **K3s 1.30+ with Traefik
and the CloudNativePG operator** — see the chart's defaults
(`ingress.className: traefik`, `postgres.image: ghcr.io/cloudnative-pg/postgresql:16.4`).

Plain Kubernetes (kind, EKS, GKE, AKS, …) works equally well; you'll
likely want to override `ingress.className` for your ingress controller.

## Architecture

The chart deploys two microservices with strict secret separation:

| Service            | Port | Role                          | Bank credentials |
| ------------------ | ---- | ----------------------------- | ---------------- |
| `comdirect-api`    | 8000 | Public-facing read-only API   | **No**           |
| `comdirect-worker` | 8001 | Internal sync + export worker | **Yes**          |

A NetworkPolicy ([`chart/templates/network-policy.yaml`](../chart/templates/network-policy.yaml))
restricts ingress to the worker so only the api can reach it. The two
services share a PVC: the worker mounts it read-write to write CSV/JSON
exports, the api mounts it read-only to serve them.

A third deployment (`comdirect-ui`) hosts the Vite-built frontend behind
the same Ingress at `/`, with `/api/*` routed to the api service. Disable
with `ui.enabled: false` in your values if you only want the API.

## Prerequisites

### Required

- Kubernetes 1.28+. K3s, k0s, kind, and the major managed offerings all work.
- An ingress controller. The chart defaults to `ingress.className: traefik`
  (K3s default); set `ingress.className` to `nginx`, `contour`, etc. as
  appropriate.
- The [CloudNativePG operator](https://cloudnative-pg.io/documentation/current/installation_upgrade/),
  installed cluster-wide. The chart provisions Postgres via a CNPG
  `Cluster` CRD when `postgres.enabled: true` (the default):

  ```bash
  kubectl apply --server-side -f \
    https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/main/releases/cnpg-1.24.1.yaml
  ```

  If you want to bring your own Postgres instead, set
  `postgres.enabled: false` and provide a Secret named in
  `postgres.credentialsSecret` containing a `uri` key with a
  `postgresql+psycopg://user:pass@host:5432/db` connection string.

### Optional

- [cert-manager](https://cert-manager.io/) — for TLS termination at the
  Ingress. Configure via `ingress.tls` and your own Issuer.
- [ExternalDNS](https://kubernetes-sigs.github.io/external-dns/) — to
  publish your `ingress.host` automatically.
- [ExternalSecrets Operator](https://external-secrets.io/) + a backend
  (Vault, AWS SM, …) — for secret rotation. See "Path B: ExternalSecrets"
  below.

## Install path A — minimal (BYO Secret, no Vault)

This is the path most external operators want: hand-managed Kubernetes
Secrets, no operator dependencies beyond the ingress controller and CNPG.

### 1. Create the namespace and Secrets

```bash
kubectl create namespace k-fin

# Bank credentials — only the worker reads these.
kubectl -n k-fin create secret generic k-fin-k-fin-comdirect-secrets \
  --from-literal=COMDIRECT_CLIENT_ID=... \
  --from-literal=COMDIRECT_CLIENT_SECRET=... \
  --from-literal=COMDIRECT_USERNAME=... \
  --from-literal=COMDIRECT_PIN=...

# Infrastructure secrets — read by both api and worker.
kubectl -n k-fin create secret generic k-fin-k-fin-infra-secrets \
  --from-literal=API_TOKEN=$(openssl rand -hex 32) \
  --from-literal=JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=BOOTSTRAP_USER_EMAIL=you@example.com \
  --from-literal=BOOTSTRAP_USER_INITIAL_PASSWORD=$(openssl rand -base64 18) \
  --from-literal=ANTHROPIC_API_KEY=        # optional, leave empty if unused
```

> **Naming.** The chart computes Secret names as `<fullname>-comdirect-secrets`
> and `<fullname>-infra-secrets`, where `<fullname>` defaults to
> `<release>-k-fin`. The example above uses release name `k-fin`, so the
> Secret name becomes `k-fin-k-fin-infra-secrets`. Pick a shorter release
> name to avoid the doubled prefix (e.g. `helm install fin ./chart` →
> `fin-k-fin-infra-secrets`).

### 2. Write a minimal `values.yaml`

```yaml
# my-values.yaml
ingress:
  enabled: true
  className: traefik          # or nginx, contour, etc.
  host: k-fin.example.com
  tls:
    enabled: false            # set to true once you wire cert-manager

api:
  env:
    APP_ENV: production
    BOOTSTRAP_LOGIN_ENABLED: "false"
    BOOTSTRAP_USER_DISPLAY_NAME: "You"
    CORS_ORIGINS: "https://k-fin.example.com"

worker:
  env:
    APP_ENV: production

# Postgres — keep CloudNativePG enabled, set a real password, and turn
# off the S3 backup unless you have a target.
postgres:
  enabled: true
  # Generated once and stored in the auto-created
  # `<release>-postgres-credentials` Secret. Generate a strong value:
  #   openssl rand -base64 24
  devPassword: REPLACE_WITH_A_STRONG_RANDOM_PASSWORD
  backup:
    enabled: false
  externalSecret:
    enabled: false            # use the chart-generated dev credentials Secret

# ExternalSecrets off — the chart consumes the two Secrets we created
# manually in step 1.
externalSecret:
  enabled: false
```

### 3. Install

```bash
helm upgrade --install k-fin ./chart -n k-fin -f my-values.yaml
```

Watch the migration hook complete:

```bash
kubectl -n k-fin get jobs --watch
kubectl -n k-fin logs -l app.kubernetes.io/component=migrate -f
```

When the deployments are ready, browse to `https://k-fin.example.com` and
log in with `BOOTSTRAP_USER_EMAIL` / `BOOTSTRAP_USER_INITIAL_PASSWORD`.

## Install path B — with ExternalSecrets + Vault

If you already operate Vault and ESO, the chart can pull bank credentials
and infrastructure secrets directly. See `chart/values.yaml` lines 143–197
for the full schema, and `dev/values.remote.example.yaml` for a worked
example.

The basics:

```yaml
externalSecret:
  enabled: true
  secretStore:
    name: vault-finance         # SecretStore scoped to secret/finance/*
    kind: SecretStore
  vaultPath: k8s/finance-api
  keys:
    - { path: finance/comdirect, vaultKey: COMDIRECT_CLIENT_ID,     envVar: COMDIRECT_CLIENT_ID }
    - { path: finance/comdirect, vaultKey: COMDIRECT_CLIENT_SECRET, envVar: COMDIRECT_CLIENT_SECRET }
    - { path: finance/comdirect, vaultKey: COMDIRECT_USERNAME,      envVar: COMDIRECT_USERNAME }
    - { path: finance/comdirect, vaultKey: COMDIRECT_PIN,           envVar: COMDIRECT_PIN }
  infraSecrets:
    secretStore:
      name: vault-backend       # broader ClusterSecretStore for infra creds
      kind: ClusterSecretStore
    keys:
      - { path: k8s/finance-api, vaultKey: API_TOKEN,                       envVar: API_TOKEN }
      - { path: k8s/finance-api, vaultKey: JWT_SECRET,                      envVar: JWT_SECRET }
      - { path: k8s/finance-api, vaultKey: BOOTSTRAP_USER_EMAIL,            envVar: BOOTSTRAP_USER_EMAIL }
      - { path: k8s/finance-api, vaultKey: BOOTSTRAP_USER_INITIAL_PASSWORD, envVar: BOOTSTRAP_USER_INITIAL_PASSWORD }
      - { path: k8s/finance-api, vaultKey: ANTHROPIC_API_KEY,               envVar: ANTHROPIC_API_KEY }
```

The chart will create both `comdirect-secrets` and `infra-secrets` from
ESO; you don't need to `kubectl create secret` anything. Same trick for
Postgres credentials: set `postgres.externalSecret.enabled: true`.

## Operations

### Upgrades

```bash
helm upgrade k-fin ./chart -n k-fin -f my-values.yaml
```

The migrate job re-runs as a `pre-upgrade` hook with idempotent
`alembic upgrade head` — safe to run on every release.

### Backups

The chart includes a `ScheduledBackup` resource (`postgres.backup.enabled`)
for CloudNativePG → S3-compatible storage. Disabled by default in this
guide because it needs an S3 target. To enable:

```yaml
postgres:
  backup:
    enabled: true
    schedule: "0 0 2 * * *"           # daily at 02:00 UTC
    retentionPolicy: "7d"
    destinationPath: "s3://my-bucket/k-fin"
    endpointURL: "https://s3.eu-central-1.amazonaws.com"   # or MinIO etc.
    credentialsSecret: k-fin-postgres-backup-s3
```

Create `k-fin-postgres-backup-s3` with `ACCESS_KEY_ID` and
`ACCESS_SECRET_KEY` keys before enabling.

### Sync workflow (pushTAN reality check)

Comdirect requires manual pushTAN approval for every session. There is no
unattended read-only token flow. In practice this means:

- Triggering a sync (UI button or `POST /internal/sync/start` against the
  worker) sends a push to your phone within seconds.
- You confirm in the Comdirect app, then the sync proceeds.
- The worker holds the pending session in memory only — if the worker
  pod restarts mid-flow, restart the sync.

**Do not run the worker as an unattended CronJob.** The auth model
doesn't support it, and bypassing pushTAN is the one boundary the
maintainer will not break.

## Reference

| Helm value                | Default       | Purpose                                                       |
| ------------------------- | ------------- | ------------------------------------------------------------- |
| `ingress.host`            | `k-fin.local` | Hostname for the Ingress.                                     |
| `ingress.className`       | `traefik`     | Override for non-K3s clusters.                                |
| `ingress.tls.enabled`     | `false`       | Enable once cert-manager is wired up.                         |
| `postgres.enabled`        | `true`        | Provision Postgres via CloudNativePG. Set `false` for BYO-DB. |
| `postgres.backup.enabled` | `true`        | Set `false` unless you have an S3 target.                     |
| `externalSecret.enabled`  | `false`       | Use ESO + Vault. Leave `false` for BYO-Secret.                |
| `ui.enabled`              | `true`        | Set `false` to skip the frontend.                             |
| `api.env.APP_ENV`         | `production`  | Hard-disables `BOOTSTRAP_LOGIN_ENABLED` when set.             |
| `api.env.CORS_ORIGINS`    | `""`          | Comma-separated; empty omits CORS middleware.                 |

For everything else, read [`chart/values.yaml`](../chart/values.yaml) — it
is heavily commented.

## See also

- [`README.md`](../README.md) — project overview and Docker Compose
  quick-start.
- [`SECURITY.md`](../SECURITY.md) — security model and reporting policy.
- [`docs/local-development.md`](local-development.md) — the maintainer's
  Tilt + Vault workflow (not needed for installs).
