## Summary

<!-- One or two sentences. What changed and why. -->

## Test plan

- [ ] `uv run ruff check .` passes
- [ ] `uv run pytest` passes
- [ ] New behavior covered by tests (or N/A — explain why)
- [ ] Manual verification (if applicable): describe what you ran

## Checklist

- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
      (`feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, `ci`)
- [ ] No secrets, IBANs, account numbers, balances, tokens, or PINs in the diff
      (gitleaks will block the commit; double-check fixtures and logs)
- [ ] Docs updated where relevant (`README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`,
      `.env.example`, chart values)
- [ ] If adding a new env var or credential pattern, `.gitleaks.toml` updated

## Related issues

<!-- Closes #123 / Refs #456 -->
