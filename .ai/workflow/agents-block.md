# >>> AI WORKFLOW MANAGED BLOCK >>>

## AI workflow integration

This repository uses the following workflow:
- Sonnet = planning, decomposition, repo adaptation
- Codex = scoped implementation
- Opus = escalation and review

Workflow rules:
1. Preserve existing repository-specific instructions in this file.
2. Prefer the smallest correct change.
3. Do not broaden scope silently.
4. Touch only relevant files unless blocked.
5. If blocked, stop and report the blocker.
6. Use `.ai/project.yaml` for commands, boundaries, and ownership.
7. Use `.ai/memory.md` for operational discoveries.
8. Use `.ai/decisions.md` for stable architectural decisions.
9. After implementation, report:
   - summary
   - files changed
   - validation
   - risks
   - assumptions

# <<< AI WORKFLOW MANAGED BLOCK <<<