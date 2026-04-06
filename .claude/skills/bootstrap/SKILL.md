---
name: bootstrap
description: Adapt the workflow scaffold to the current repository. Use only when onboarding a repo or when project metadata is incomplete or stale.
---

You are the bootstrap skill.

Goal:
Adapt the workflow scaffold to the current repository without implementing product changes.

You must:
1. Detect the stack, package managers, and likely entrypoints.
2. Identify install, dev, build, test, lint, format, and typecheck commands.
3. Identify major directories and assign rough ownership domains.
4. Identify risky areas, generated files, do-not-touch zones, and security-sensitive areas.
5. Update only:
   - AGENTS.md (only project-specific sections if needed)
   - CLAUDE.md (only project-specific sections if needed)
   - .ai/project.yaml
   - .ai/memory.md
6. Preserve the fixed workflow roles:
   - Sonnet = planner/orchestrator
   - Codex = executor
   - Opus = escalation/reviewer
7. Record uncertainty explicitly in `assumptions` and `unknowns`.
8. Do not implement features or fixes.

Output format:
- Detected stack
- Commands
- Important dirs/files
- Risks and boundaries
- Updated files
- Assumptions
- Unknowns
