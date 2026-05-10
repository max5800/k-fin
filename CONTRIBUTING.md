# Contributing to k-fin

k-fin handles real banking credentials and financial data. Contributions are welcome,
but the bar for security and review is non-negotiable. Read this whole file before
opening a PR.

## Local development

- Setup, runtime, and architecture live in [`README.md`](README.md).
- Project conventions, agent layout, and module map live in [`CLAUDE.md`](CLAUDE.md).
- Architecture overview: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- Self-hosting + cluster topology: [`docs/kubernetes-deployment.md`](docs/kubernetes-deployment.md)
  and [`docs/local-development.md`](docs/local-development.md).

Quick checks before pushing:

```bash
uv run ruff check .
uv run pytest
```

CI runs the same commands plus Alembic migration smoke tests and Docker builds.

## Branches

- Cut from `main`. There is no long-lived `develop` branch.
- Naming: `<type>/<short-kebab-summary>`, where `<type>` matches the conventional-commit
  type. Examples from the repo history:
  - `feat/m6-finance-api-core`
  - `fix/external-params`
  - `chore/repo-hygiene-m14`
  - `docs/architecture-skeleton`
  - `refactor/phase0-defirefly-v2`

Keep branches focused. Split large work into reviewable PRs.

## Commits

- **Conventional Commits are mandatory.** semantic-release reads them to cut versions
  and write the changelog. See [Conventional Commits](https://www.conventionalcommits.org/).
- Common types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, `ci`.
- Use a scope when it helps: `feat(api): ...`, `fix(external): ...`, `chore(deploy): ...`.
- Subject line in the imperative, lowercase, no trailing period, ≤72 chars.
- `commitlint` runs on commit (via husky) and rejects messages that do not parse.

## Pre-commit hook: gitleaks (mandatory)

Every commit is scanned for secrets by `gitleaks` via `.husky/pre-commit`. Install it
once:

```bash
brew install gitleaks
```

Rules and allowlists live in [`.gitleaks.toml`](.gitleaks.toml). If gitleaks fires:

1. **Real secret** — remove it, rotate the credential, move the value into `.env` or
   Vault. Never amend a commit that already contains a secret; consider it leaked.
2. **False positive** — add a narrow allowlist entry to `.gitleaks.toml` with a
   comment explaining why it is safe. Do not widen existing allowlists casually.

**Do not bypass with `git commit --no-verify`.** CI re-runs gitleaks on the PR; a leak
that escapes the hook still ends up on the public repo.

See [`SECURITY.md`](SECURITY.md) for the threat model and disclosure policy.

## Tests

- New behavior needs tests. Bug fixes need a regression test.
- `uv run pytest` must be green locally and in CI before review.
- Use obvious dummy data: IBAN `DE00000000000000000000`, name `John Doe`, amounts
  unrelated to anything personal. Never paste real bank data into fixtures or logs.
- The Comdirect connector is strictly read-only. Tests must not exercise write paths
  against the real API; mock the client instead.

## PR workflow

1. Fork the repo (external contributors) or push a branch (maintainers).
2. Branch off `main` using the naming above.
3. Open a PR against `main`. Fill in the PR template; keep the diff focused.
4. CI must pass: lint, tests, migrations, Docker build, gitleaks.
5. Squash-merge or rebase-merge — `main` keeps a linear history.

## Reporting security issues

**Do not open a public issue for vulnerabilities.** See [`SECURITY.md`](SECURITY.md)
for the private disclosure channel. This includes anything that touches bank
credentials, token handling, auth, or data leakage.
