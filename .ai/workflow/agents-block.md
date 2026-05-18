# >>> AI WORKFLOW MANAGED BLOCK >>>

## AI workflow integration

This repository uses an AI workflow. See `.ai/workflow/workflow.md` for full pipeline details.

Roles: configured in `.ai/models.yaml`

## Orchestration entry point

For any development task, follow the `orchestrate` skill:
- Claude: invoke "Use the orchestrate skill. Task: <description>" (skill at `.claude/skills/orchestrate/SKILL.md`)
- Codex: read `~/.agents/skills/orchestrate/SKILL.md` and follow it (installed globally by `install.sh`)

Both paths resolve to the same pipeline contract; only the discovery path differs.

Execution rules:
1. Prefer the smallest correct change; touch only relevant files; do not broaden scope silently.
2. If blocked, stop and report the blocker.
3. Fill the Handoff section before declaring done.
4. After implementation, report: summary, files changed, validation, risks, assumptions.
5. Never execute delete or mass-removal commands (rm -rf, rmdir, del, Remove-Item, git clean). List required deletions in Handoff `Pending deletions` — the orchestrator executes them after review.
6. Never commit/push history; the orchestrator owns that after review.

# <<< AI WORKFLOW MANAGED BLOCK <<<
