---
name: k-fin-security-check
description: Use when the user asks for /security-check, a k-fin security audit, the security-reviewer agent, or a banking-data AppSec pass in this repo.
---

# k-fin Security Check

Run the Codex equivalent of `.claude/commands/security-check.md`.

## Source files

- Command: `.claude/commands/security-check.md`
- Agent registry: `AGENTS.md`
- Agent definition: `.claude/agents/security-reviewer.md`

## Workflow

1. Read `AGENTS.md`, `.claude/commands/security-check.md`, and
   `.claude/agents/security-reviewer.md`.
2. Treat `/security-check` as explicit authorization to use the
   `security-reviewer` as a Codex `explorer` subagent.
3. Review the whole codebase for:
   - hardcoded secrets, tokens, credentials, PINs, and realistic IBANs
   - sensitive logging of account numbers, IBANs, balances, transactions, or tokens
   - missing auth, unsafe path handling, or overbroad CORS on API endpoints
   - Docker/chart/workflow paths that leak secrets or break api/worker isolation
   - secret-scanning gaps in `.gitleaks.toml`, `.husky/pre-commit`, and CI
4. Return findings in the format requested by the source agent definition.

If no issues are found, say that clearly and include the residual scope checked.
