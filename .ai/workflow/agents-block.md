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
7. Never execute delete or mass-removal commands (rm -rf, rmdir, del, Remove-Item, git clean, etc.). List any required deletions in the Handoff `Pending deletions` field — the orchestrator executes them after review.
8. Never commit changes. Do not run git commit, git push, or any command that records or publishes history. Committing is the orchestrator's responsibility after review.

# <<< AI WORKFLOW MANAGED BLOCK <<<
