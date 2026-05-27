# Codex Agent Registry - k-fin

This file bridges the existing Copilot and Claude Markdown definitions into Codex.
Keep the source definitions in their native locations and treat this file as the
Codex-facing index.

## Source Definitions

- Project rules: `CLAUDE.md`
- Copilot rules: `.github/copilot-instructions.md`
- Copilot commit instructions: `.github/instructions/conventional-commits.instructions.md`
- Claude subagents: `.claude/agents/*.md`
- Claude commands: `.claude/commands/*.md`

When the same rule appears in multiple places, follow the stricter security or
privacy rule. If documentation conflicts with code, inspect the current code and
preserve the banking/security invariants from `CLAUDE.md`.

## Project Invariants

- This is a public OSS repo handling sensitive banking data. Never introduce real
  IBANs, balances, credentials, hostnames, tokens, PINs, or personal infra paths.
- Comdirect access is read-only. Do not add bank-state mutation features.
- Keep `k-fin` lowercase and hyphenated in code, UI, logs, commits, and docs.
- Secrets must come from environment variables, local git-ignored files, or
  Vault/ESO. Do not put secrets in committed YAML, Dockerfiles, fixtures, or docs.
- Use obvious dummy data in tests and examples, such as
  `DE00000000000000000000` and `John Doe`.
- Use `uv run ...` for Python commands. Before declaring implementation work done,
  prefer `uv run pytest` and `uv run ruff check .` when scope and time allow.
- Conventional commits are required; see
  `.github/instructions/conventional-commits.instructions.md`.

## Claude Agents Available To Codex

Codex cannot install these Markdown files as native agent types. When the user
asks for one of these agents, read the corresponding file and instantiate the
closest Codex subagent role with that Markdown as the task prompt.

| Requested agent | Source | Codex role | Use for |
| --- | --- | --- | --- |
| `security-reviewer` | `.claude/agents/security-reviewer.md` | `explorer` | AppSec and banking-data security review |
| `platform-reviewer` | `.claude/agents/platform-reviewer.md` | `explorer` | Docker, CI/CD, Helm, infra review |
| `code-reviewer` | `.claude/agents/code-reviewer.md` | `explorer` | Python quality, correctness, test coverage review |
| `architect` | `.claude/agents/architect.md` | `explorer` | Module boundaries, data flow, API design |
| `test-engineer` | `.claude/agents/test-engineer.md` | `worker` | Writing/running tests and verification patches |
| `deployment-engineer` | `.claude/agents/deployment-engineer.md` | `worker` | Helm/K3s/Vault/ESO/Docker/release changes |
| `plan-status` | `.claude/agents/plan-status.md` | `worker` | Reconciling the canonical Obsidian project plan |

For read-only analysis, prefer `explorer`. For bounded code or config changes,
prefer `worker` and give it explicit file/module ownership. Agents are not alone
in the worktree; they must preserve user edits and coordinate with concurrent
changes.

## Claude Command Equivalents

These entries are adapters. Do not duplicate command behavior here; read the
source command Markdown and apply the agent mapping above.

- `/full-review`: read `.claude/commands/full-review.md`, then instantiate the
  requested Claude agents through the Codex roles listed above.
- `/security-check`: read `.claude/commands/security-check.md`, then instantiate
  `security-reviewer` through the Codex role listed above.
- `/plan-status`: read `.claude/commands/plan-status.md`, then instantiate
  `plan-status` through the Codex role listed above and pass along any milestone
  argument exactly.

If a Codex skill or plugin summarizes one of these commands, the source
`.claude/commands/*.md` file is still authoritative. Preserve its scope rules
exactly, including dirty working-tree files, staged changes, untracked files, and
changes since the latest release tag.

## Claude Hook Parity

Codex does not execute `.claude/settings.json` hooks automatically. To preserve
the Claude Code safety model, manually apply the configured Stop hook before
finishing any implementation, commit, or review that touched files:

1. Identify files modified in this session.
2. Run a quick `security-reviewer`-perspective scan over those files for
   hardcoded secrets, credentials in logs, sensitive data exposure, and path
   traversal risks.
3. Report actual issues only. If clean, state that the modified files passed the
   security hook check.

Only spawn Codex subagents when the user explicitly asks for agents, delegation,
or parallel agent work. Otherwise, apply the relevant role instructions locally.
