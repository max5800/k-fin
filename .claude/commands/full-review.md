Run a full team review of the project. Spawn the following agents in parallel:

1. **security-reviewer** — Full security audit of the codebase
2. **platform-reviewer** — Review Docker, dependencies, and infrastructure
3. **code-reviewer** — Review code quality and test coverage
4. **architect** — Evaluate module structure and data flow

After all agents complete, compile a unified report with:
- Combined findings sorted by severity
- Cross-cutting concerns (issues flagged by multiple agents)
- Top 5 action items prioritized by impact

Focus the review on files changed since the last release tag, or the full codebase if no tags exist.
