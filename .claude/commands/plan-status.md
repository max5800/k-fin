Reconcile the canonical project plan with the actual repo state.

Spawn the `plan-status` subagent via the Task tool. If the user passed an
argument (e.g. `/plan-status M11` or `/plan-status M7a`), forward it as the
milestone identifier so the agent only walks that block. With no argument,
the agent walks the entire plan.

After the subagent returns:
1. Pass through its report verbatim — it already has the structured format.
2. Remind the user how to inspect the diff before committing the obsidian
   vault: `git -C "/Users/max/workspace/obsidian_vault" diff "Tech/Firefly & Finanz Sync/00_PROJEKTPLAN_KANONISCH.md"`
