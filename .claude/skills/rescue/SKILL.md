---
name: rescue
description: Recover from failed implementation attempts by isolating wrong assumptions and proposing the next narrow experiment.
---

You are the rescue skill.

Use this skill after repeated failure, unclear regressions, or when implementation drift has started.

## Prerequisite

Check `.ai/project.yaml`. If `project_name` is `unknown` and `stack` is empty, STOP and tell the user: "Run the bootstrap skill first. The project metadata is empty."

## Rules

1. Do not continue patching blindly.
2. Identify which assumptions are likely wrong — assign confidence: high | medium | low.
3. Separate evidence from speculation.
4. Propose the narrowest next experiment — one small, testable step.
5. Escalate to the model assigned to `review` in `.ai/models.yaml` if the failure is architectural or cross-cutting.

## Token budget

Rescue output ≤40 lines.

## Output format

- What failed
- Wrong assumptions (each with confidence level)
- Evidence (paste actual logs/errors)
- Safer fallback
- Next experiment
- Escalation recommendation
