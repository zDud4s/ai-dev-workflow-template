# >>> AI WORKFLOW MANAGED BLOCK >>>

## AI workflow integration

This repository uses an AI workflow. See `.ai/workflow/claude-workflow.md` for full pipeline details.

Roles: configured in `.ai/models.yaml`

Execution rules:
1. Prefer the smallest correct change.
2. Do not broaden scope silently.
3. Touch only relevant files unless blocked.
4. If blocked, stop and report the blocker.
5. Fill the Handoff section in the execution packet before declaring done.
6. After implementation, report: summary, files changed, validation, risks, assumptions.

# <<< AI WORKFLOW MANAGED BLOCK <<<
