---
name: k-fin-plan-status
description: Use when the user asks for /plan-status, plan-status reconciliation, checking off the canonical k-fin project plan, or running the plan-status agent with an optional milestone like M11.
---

# k-fin Plan Status

Run the Codex equivalent of `.claude/commands/plan-status.md`.

## Source files

- Command: `.claude/commands/plan-status.md`
- Agent registry: `AGENTS.md`
- Agent definition: `.claude/agents/plan-status.md`

## Workflow

1. Read `AGENTS.md`, `.claude/commands/plan-status.md`, and
   `.claude/agents/plan-status.md`.
2. Treat `/plan-status` as explicit authorization to use the `plan-status` Codex
   `worker` subagent.
3. Pass along any milestone argument exactly, for example `M11` or `M7a`.
4. The worker owns only the canonical plan reconciliation described in
   `.claude/agents/plan-status.md`.
5. The worker may edit only the plan file and only in the allowed directions:
   `[ ]` to `[x]`, and milestone heading `🔲` to `✅`.
6. The worker must verify each checkbox against the real backend/UI code before
   flipping it and must leave vague or externally unverifiable items open.
7. Return the report format defined in `.claude/agents/plan-status.md`.

The canonical plan lives outside this repo. If filesystem permissions block that
edit, ask for approval rather than changing a copy.
