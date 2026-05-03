# Local Development (Maintainer Workflow)

> **Heads-up:** this document describes the **maintainer's** development
> workflow against a remote K3s cluster with Tilt + Vault + ExternalSecrets.
> If you just want to **install or run** k-fin, you don't need any of this.
> See:
>
> - [`README.md`](../README.md) → "Run with Docker Compose" — fastest path,
>   no Kubernetes required.
> - [`docs/kubernetes-deployment.md`](kubernetes-deployment.md) — generic
>   Helm install guide (BYO-Secret path included, no Vault assumed).

---

## What this workflow looks like

The maintainer's dev loop runs on a Mac with **Rancher Desktop** providing
the local container runtime, against a remote **K3s cluster** on a homelab
mini-server. The cluster itself — K3s install, operators, namespaces,
TLS, RBAC — is provisioned with OpenTofu + Ansible and reconciled
continuously by **Rancher Fleet** (Rancher's built-in GitOps controller),
with **Rancher Manager** providing the admin UI on top. k-fin's *application*
deployment loop is driven separately by **Tilt** so source changes hit
running pods without waiting for a Fleet sync.

Concretely:

- **Workstation (Mac)**: Rancher Desktop supplies the Docker daemon and
  the `kubectl`/`helm`/`nerdctl` toolchain. The active kubectl context
  is `k3s-app`, pointing at the homelab cluster.
- **Cluster (homelab mini-server)**: K3s on a Proxmox VM, registered
  with Rancher Manager. Cluster infrastructure — CloudNativePG,
  ExternalSecrets Operator, Traefik, cert-manager, ExternalDNS, Vault
  integration — is reconciled by Fleet from a separate (also public)
  GitOps repo: <https://github.com/max5800/home-lab>. k-fin's
  `Deployment`/`Service`/`Ingress` resources are *not* in that repo;
  they're owned by the Helm chart in **this** repo.
- **Dev deployment**: `tilt up --stream` builds Docker images locally,
  pushes them to GHCR, applies the chart to the `k-fin` namespace on
  `k3s-app`, and live-syncs source changes into the running pods.
- **Production deployment**: a Fleet `GitRepo` in the home-lab repo
  pins a tagged chart version from `oci://ghcr.io/max5800/helm-charts/k-fin`
  and deploys it on chart releases — no Tilt involvement.
- **Secret access**: bank credentials and infra secrets live in
  HashiCorp Vault. ESO (deployed by Fleet) projects them into
  Kubernetes Secrets that the chart's deployments consume via `envFrom`.

This is opinionated — Vault, ESO, Fleet, Rancher Manager, a remote K3s
context, and a `dev/values.local.yaml` with real ingress hostnames are
all assumed to exist. None of it is required to **deploy** k-fin
somewhere else.

## Prerequisites

### On the Mac (workstation)

- [Rancher Desktop](https://rancherdesktop.io/) — provides the local
  Docker daemon, kubectl, helm, and nerdctl. (Docker Desktop +
  standalone kubectl works equally well; Rancher Desktop is the
  maintainer's choice for consistency with Rancher Manager on the
  cluster side.)
- [Tilt](https://docs.tilt.dev/install.html).
- [Helm 3](https://helm.sh/docs/intro/install/) (bundled with Rancher
  Desktop).
- A kubectl context named `k3s-app` pointing at the homelab cluster
  (the Tiltfile pins to this context name via `allow_k8s_contexts`).
- The sibling repo [`k-fin-ui`](https://github.com/max5800/k-fin-ui)
  checked out at `../k-fin-ui` — Tilt builds the UI from there.

### On the remote K3s cluster (Fleet-managed)

These are cluster-side prerequisites; they live in the maintainer's
home-lab GitOps repo (<https://github.com/max5800/home-lab>), not in
this repo. If you're rebuilding the homelab from scratch, Fleet will
install them — but you can also install each manually if you don't run
Fleet:

- A K3s cluster reachable from the workstation. The maintainer's runs
  on a Proxmox VM provisioned by OpenTofu and bootstrapped by Ansible;
  any K3s install works.
- Rancher Manager (optional) — only used for the admin UI and
  per-cluster RBAC. Fleet itself can run standalone.
- [CloudNativePG operator](https://cloudnative-pg.io/documentation/current/installation_upgrade/),
  installed cluster-wide.
- [ExternalSecrets Operator](https://external-secrets.io/latest/introduction/getting-started/)
  with a Vault backend and two SecretStores:
  - `vault-finance` (`SecretStore`, scoped to `secret/data/finance/*`) —
    holds Comdirect credentials.
  - `vault-backend` (`ClusterSecretStore`, broader scope) — holds
    `API_TOKEN`, `JWT_SECRET`, `BOOTSTRAP_*`, `ANTHROPIC_API_KEY`,
    and Postgres bootstrap credentials.
- Traefik ingress controller (K3s default) and cert-manager (for the
  Let's Encrypt issuer used by the Ingress).

## First-time setup

Create your local values file from the template (the file is git-ignored):

```bash
cp dev/values.remote.example.yaml dev/values.local.yaml
$EDITOR dev/values.local.yaml
```

In `dev/values.local.yaml`, set at minimum:

- `ingress.host` — your dev hostname (e.g. `k-fin-dev.example.com`).
- `api.env.CORS_ORIGINS` and `worker.env.CORS_ORIGINS` — match the
  hostname(s) the UI is reached from.
- `ui.env.VITE_ALLOWED_HOSTS` — same hostname (Vite v6 rejects unknown
  Host headers).
- `externalSecret.vaultPath` — your Vault KV path layout if it differs
  from the example.

Write the secrets Vault expects:

```bash
# Comdirect bank credentials (eso-comdirect role)
vault kv put secret/finance/comdirect \
  COMDIRECT_CLIENT_ID=... \
  COMDIRECT_CLIENT_SECRET=... \
  COMDIRECT_USERNAME=... \
  COMDIRECT_PIN=...

# Infrastructure secrets (eso-reader role)
vault kv put secret/k8s/finance-api \
  API_TOKEN=... \
  ANTHROPIC_API_KEY=sk-ant-... \
  JWT_SECRET=...   # 32+ char random string \
  BOOTSTRAP_USER_EMAIL=you@example.com \
  BOOTSTRAP_USER_INITIAL_PASSWORD=...   # 12+ chars

# Postgres bootstrap credentials
vault kv put secret/k8s/finance-postgres \
  username=finance password=...
```

## Running the dev loop

```bash
tilt up --stream
```

The Tiltfile will:

1. Verify `dev/values.local.yaml` exists and `ingress.host` is set.
2. Build three images (`k-fin-api`, `k-fin-worker`, `k-fin-migrate`) from
   `Dockerfile.api` / `Dockerfile`, and the UI from `../k-fin-ui/Dockerfile.dev`.
3. Apply the chart via `helm template … | kubectl apply -f -`.
4. Sync `src/`, `main.py`, and `scripts/` into running pods on save
   (no rebuild round-trip).

The Tilt dashboard links directly to Swagger (`/docs`), ReDoc (`/redoc`),
and `/health` on your `ingress.host`.

## Standalone Helm (no Tilt)

If you want to deploy without Tilt's live-update loop:

```bash
helm upgrade --install k-fin-dev ./chart \
  -n k-fin --create-namespace \
  -f dev/values.local.yaml
```

Same chart, same values file — Tilt is purely a dev-loop accelerator.

## Day-to-day commands

The user-facing commands live in [`CLAUDE.md`](../CLAUDE.md), but the most
common ones:

```bash
uv sync                                  # install/update deps
uv run pytest                            # run tests
uv run ruff check .                      # lint
uv run alembic revision -m "..."         # new migration
uv run python scripts/export_csv.py --output-dir exports  # local export
```

## Releasing

Conventional commits drive `semantic-release`. A push to `main` with a
`feat:` or `fix:` commit will:

1. Bump the chart `Chart.yaml` version.
2. Tag the repo.
3. Build & push `ghcr.io/max5800/k-fin-{api,worker}:vX.Y.Z`.
4. Update `CHANGELOG.md`.

The UI (`k-fin-ui`) releases independently with its own
`semantic-release` config; bump `chart/values.yaml` `ui.image.tag` manually
after a new UI release.
