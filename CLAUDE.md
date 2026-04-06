# CLAUDE.md

Use the workflow scaffold in this repository.

Primary sources of truth:
- `AGENTS.md` for repo-wide operating rules
- `.ai/project.yaml` for project facts, commands, boundaries, and conventions
- `.ai/memory.md` for operational discoveries
- `.ai/decisions.md` for stable architecture decisions
- `.claude/skills/*` for reusable Claude behaviors

Role split:
- Sonnet handles planning, decomposition, repo adaptation, and task routing
- Codex handles scoped implementation
- Opus handles escalation, architecture review, and failure analysis

Claude-specific rules:
1. Prefer narrow plans and narrow execution packets.
2. Use the planner skill for any task larger than a tiny local edit.
3. Use the bootstrap skill only for onboarding or when project metadata is stale.
4. Use the reviewer skill for risky, cross-cutting, or high-consequence changes.
5. Use the rescue skill after repeated failure or implementation drift.
6. Keep prompts compact and operational.
7. Mark uncertainty explicitly as assumptions or unknowns.
8. Do not let bootstrap become product work.

Expected packet flow:
- plan
- execute
- review
- rescue when needed
