---
description: "Use when writing git commit messages, committing code, or preparing changelogs. Enforces Conventional Commits format with project-specific types and scopes."
---
# Conventional Commits

This project enforces [Conventional Commits](https://www.conventionalcommits.org/) via commitlint + husky.

## Format

```
<type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

## Allowed Types

`feat` `fix` `docs` `style` `refactor` `perf` `test` `chore` `revert` `ci` `build`

## Scopes (optional)

Derive from module: `connector`, `api`, `importer`, `exporter`, `scheduler`, `core`, `auth`, `scripts`, `docker`, `release`

## Rules

- Subject line: lowercase, imperative, no period, max ~72 chars
- `feat` and `fix` trigger a release (semantic-release)
- Use `!` after type/scope for breaking changes: `feat!: remove legacy export`
- Body: wrap at 100 chars (soft rule, not enforced)
- Prefer atomic commits — one logical change per commit
- When multiple files change for one feature, use a single `feat` commit with a body listing key changes

## Examples

```
feat(api): add health check endpoint
fix(connector): handle empty transaction list
docs: update README with Docker instructions
chore(docker): bump base image to python 3.13
refactor(core): extract config validation
ci: add semantic-release GitHub Actions workflow
```
