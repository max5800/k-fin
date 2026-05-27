Run a full team review of the project. Spawn the following agents in parallel:

1. **security-reviewer** — Full security audit of the codebase
2. **platform-reviewer** — Review Docker, dependencies, and infrastructure
3. **code-reviewer** — Review code quality and test coverage
4. **architect** — Evaluate module structure and data flow

After all agents complete, compile a unified report with:
- Combined findings sorted by severity
- Cross-cutting concerns (issues flagged by multiple agents)
- Top 5 action items prioritized by impact

Scope the review as follows:

1. Start with `git status --short` and include tracked modifications, staged changes,
   and untracked files/directories in the working tree.
2. Also include files changed in commits since the latest release tag
   (`git describe --tags --abbrev=0`).
3. If no release tag exists, review the full codebase.
4. If the tag diff is empty but the working tree is dirty, review the working-tree
   changes instead of treating the review as empty.
5. Include directly related source files when needed to validate the changed files'
   security, platform, code-quality, or architecture impact.
