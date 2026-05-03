# Security Policy

k-fin handles banking credentials and personal financial data. We take security reports seriously.

## Reporting a Vulnerability

**Please do not open public GitHub issues for security findings.**

Send a private report by email to:

> **[rehms.maximilian@gmail.com](mailto:rehms.maximilian@gmail.com)** — subject prefix `[k-fin security]`

Include, as much as you can:

- Affected component (connector, API, worker, normalization, agents, MCP server, Helm chart, CI workflow)
- Affected version (commit SHA or release tag)
- Steps to reproduce, expected vs. observed behavior
- Impact assessment if you have one (credential exposure, data leak, RCE, auth bypass, etc.)
- Whether you're willing to be credited in the fix announcement

You should get an acknowledgment within **5 working days**. As a personal project there is no SLA — fixes ship as time allows, but credential-exposing or remotely-exploitable issues take priority.

## Scope

In scope:

- The backend repo (this repo: `k-fin`) — connector, API, worker, agents, MCP server, normalization pipeline
- The frontend repo (`k-fin-ui`)
- The Helm chart in `chart/`
- The CI/CD workflows in `.github/workflows/`

Out of scope:

- Vulnerabilities in upstream dependencies — please report those to the upstream project. We track dependencies via Dependabot and update on a best-effort basis.
- The Comdirect REST API itself — report to Comdirect directly.
- Self-inflicted misconfiguration on a fork or self-hosted deployment (exposing the worker publicly, leaking your `.env`, weak `API_TOKEN`, etc.). The deployment guide in [docs/kubernetes-deployment.md](docs/kubernetes-deployment.md) and the architecture notes in [README.md](README.md) describe the intended security boundaries.

## Disclosure

We prefer **coordinated disclosure**:

1. Report privately as above.
2. We confirm and work on a fix.
3. We agree on a public disclosure timeline (typically when the fix is released, or 90 days after the report — whichever comes first).
4. Credit is given in the release notes unless you prefer to remain anonymous.

## Pre-commit Secret Scanning

Both `k-fin` and `k-fin-ui` enforce a two-layer secret-scanning gate to prevent credentials, real banking data, and personal infrastructure references from leaking into the public repo:

1. **Local pre-commit hook** ([.husky/pre-commit](.husky/pre-commit)) runs [gitleaks](https://github.com/gitleaks/gitleaks) against staged content using the rules in [.gitleaks.toml](.gitleaks.toml). Required tool:

   ```bash
   brew install gitleaks
   ```

   The hook fails closed if `gitleaks` is missing — install it once, then commits work normally.

2. **CI enforcement** ([.github/workflows/security.yml](.github/workflows/security.yml)) re-runs the same scan on every PR and push. This catches anything that bypasses the local hook (`git commit --no-verify`, contributor without gitleaks installed, etc.). **A failing CI scan blocks the PR.**

### Handling a finding

When the hook flags something:

- **Real secret** — remove it, move it to `.env` (git-ignored), and rotate the credential. Treat it as if it were already public; if the commit hit any remote, `git commit --amend` won't help.
- **False positive** — add a NARROW allowlist regex to `.gitleaks.toml` with a comment explaining why the value is safe (e.g., a documented dummy IBAN, a placeholder string in `.env.example`). Do NOT use inline `gitleaks:allow` comments in source — they invite lazy bypass.

### Scope of custom rules

The k-fin custom rules cover what the gitleaks defaults miss for this project:

- Comdirect bank credentials (`COMDIRECT_CLIENT_ID`, `COMDIRECT_PIN`, etc.) with non-placeholder values
- Real-looking German IBANs (`DE` + 20 digits) outside of the documented dummy set used in fixtures
- The maintainer's personal infrastructure domain (`max5800.com`) leaking back into shipped defaults
- RFC 1918 private network references and `.home.lab` hostnames
- Hardcoded `JWT_SECRET` values

The UI repo additionally guards against hardcoded production API URLs in `VITE_API_BASE_URL` and embedded `Bearer` tokens in source.

## Security Design Notes

A few invariants worth knowing before you report:

- **Read-only by design.** The Comdirect connector implements only `GET`. Reports of "could exploit this to issue transfers" require demonstrating a way to make the connector issue a non-GET — there is no write surface to exploit.
- **TAN-in-the-loop is intentional.** Every sync requires interactive pushTAN confirmation on the user's mobile device. Reports of "the worker should be able to refresh on a cron without TAN" are a feature request, not a vulnerability — Comdirect does not offer scoped read-only API tokens, so the manual confirmation is the security boundary.
- **Two-microservice secret separation.** Bank credentials only live on the `comdirect-worker`, never on `comdirect-api`. A Kubernetes NetworkPolicy restricts ingress to the worker. Reports of credential exposure on the API side, or paths that would let an attacker reach the worker without going through the API, are highly relevant.
- **No third-party telemetry.** k-fin does not send your data anywhere outside what you configure (Comdirect for reads, your Postgres, your S3 if backups are enabled, Anthropic if AI categorization is enabled).
