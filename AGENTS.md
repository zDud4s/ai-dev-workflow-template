# AGENTS.md

This repository uses a fixed 3-role workflow:

- Sonnet: planning, decomposition, repo adaptation, and task routing
- Codex: scoped implementation
- Opus: escalation, architecture review, and failure analysis

Operating rules:
1. Prefer the smallest correct change.
2. Do not modify unrelated files.
3. Do not broaden scope silently.
4. State assumptions explicitly.
5. If project commands or boundaries are unclear, inspect the repo and update `.ai/project.yaml` before coding.
6. When implementing, follow the execution packet exactly unless blocked.
7. If blocked, stop and report the blocker instead of guessing across the codebase.
8. After changes, report:
   - summary
   - files changed
   - risks
   - validation performed
   - assumptions made

Required project files:
- `.ai/project.yaml` for commands, entrypoints, boundaries, and conventions
- `.ai/memory.md` for discovered quirks
- `.ai/decisions.md` for stable architectural choices

Escalation policy:
- Use review/escalation for:
  - cross-cutting changes
  - auth, billing, security, infra, migrations
  - repeated failure
  - unclear architecture
  - any task touching more than one major subsystem

Implementation constraints:
- Do not rewrite large modules when a local fix is possible.
- Do not add dependencies unless explicitly justified.
- Do not change formatting or naming outside the touched scope.
- Do not move files unless the task explicitly requires it.

Output contract for implementation:
- Summary:
- Files changed:
- Validation:
- Risks:
- Follow-up needed:
