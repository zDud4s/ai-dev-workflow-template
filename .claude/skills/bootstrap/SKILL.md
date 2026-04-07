---
name: bootstrap
description: Adapt the workflow scaffold to the current repository. Use only when onboarding a repo or when project metadata is incomplete or stale.
---

You are the bootstrap skill.

Goal:
Adapt the workflow scaffold to the current repository without implementing product changes.

## You must

1. Detect the stack, package managers, and likely entrypoints.
2. Identify install, dev, build, test, lint, format, and typecheck commands. If a command cannot be confirmed from repo evidence, mark it as an assumption.
3. Identify major directories and assign rough ownership domains.
4. Identify risky areas, generated files, do-not-touch zones, and security-sensitive areas.
5. Update only:
   - AGENTS.md (only project-specific sections if needed)
   - CLAUDE.md (only project-specific sections if needed)
   - `.ai/project.yaml`
   - `.ai/memory.md`
6. Preserve the workflow role structure (planner, executor, reviewer). Do not hardcode tool or model names — those are configured in `.ai/models.yaml`.
7. Record uncertainty explicitly in `assumptions` and `unknowns`.
8. After filling `.ai/project.yaml`, prompt the user to review `.ai/models.yaml` and adjust tool/model assignments if the defaults do not match their setup.
9. If the project has clear subdirectories with distinct domains (e.g. `backend/`, `frontend/`, `packages/*`), create local AGENTS.md files for each with domain-specific constraints. Do NOT create subdirectory AGENTS.md files for projects without clear subdomain separation (CLI tools, single-purpose libraries, monoliths with flat structure). Track created files in `subdirectory_agents` in `.ai/project.yaml`.
10. Do not implement features or fixes.

## Token budget

Bootstrap output ≤150 lines.

## Output format

- Detected stack
- Commands (confirmed vs assumed)
- Important dirs/files
- Risks and boundaries
- Subdirectory AGENTS.md files created (if any)
- Updated files
- Assumptions
- Unknowns
