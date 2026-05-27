---
name: k-fin-full-review
description: Use when the user asks for /full-review, a full k-fin agent-team review, a parallel security/platform/code/architecture review, or the Claude Code full-review equivalent in Codex.
---

# k-fin Full Review

Run the Codex equivalent of `.claude/commands/full-review.md`.

## Source files

- Command: `.claude/commands/full-review.md`
- Agent registry: `AGENTS.md`
- Agent definitions: `.claude/agents/*.md`

## Workflow

1. Read `AGENTS.md` and `.claude/commands/full-review.md`.
2. Treat `.claude/commands/full-review.md` as the source of truth for reviewer
   set, review scope, and unified report shape.
3. Use `AGENTS.md` only to map each requested Claude agent to the closest Codex
   role.
4. Treat `/full-review` as explicit authorization to use parallel Codex
   subagents.
5. Ask each subagent to read its source Markdown before analyzing the repo.
6. Merge the returned reports according to `.claude/commands/full-review.md`.

Do not invent findings. If a reviewer reports no issues, keep that signal in the
summary instead of padding the report.
