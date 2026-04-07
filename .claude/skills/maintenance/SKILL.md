---
name: maintenance
description: Maintain the mutable project layer after repo changes or completed tasks. Refresh project metadata, commands, boundaries, memory, and local guidance without changing the workflow core.
---

You are the maintenance skill.

Purpose:
Keep the project-specific workflow layer accurate over time without degrading the core workflow.

## Prerequisite

Check `.ai/project.yaml`. If `project_name` is `unknown` and `stack` is empty, STOP and tell the user: "Run the bootstrap skill first. The project metadata is empty."

## Core principle

The workflow core is immutable by default. Only maintain the mutable project layer unless the user explicitly asks for a core workflow change.

Immutable core:
- root AGENTS.md workflow roles
- escalation policy
- core packet schemas
- planner / reviewer / rescue / bootstrap skill behavior
- safety boundaries

Mutable project layer:
- `.ai/project.yaml`
- `.ai/memory.md`
- `.ai/decisions.md`
- local AGENTS.md files inside project subdirectories

## When to use

- After bootstrap
- After completing a task that revealed new repo facts
- After build config, package manager, or CI changes
- After discovering new fragile modules or risky areas
- After repeated failures caused by stale project metadata
- After any review that lists memory updates to apply
- When asked to "refresh", "maintain", or "update the workflow"

## Responsibilities

1. Re-scan the repository structure when relevant.
2. Refresh detected commands if scripts, package managers, or entrypoints changed.
3. Update ownership, important directories, risky areas, and do-not-touch zones in `.ai/project.yaml`.
4. Append operational discoveries to `.ai/memory.md`.
5. Record stable architectural decisions in `.ai/decisions.md` only when evidence is strong.
6. Tighten local subdirectory AGENTS.md files if the structure clearly changed.
7. Remove stale assumptions when they are disproven.
8. Keep the project layer concise, factual, and operational.

## Never do without explicit user approval

- Rewrite root workflow roles
- Change the core escalation policy
- Loosen safety boundaries
- Rewrite planner/reviewer/rescue logic
- Silently broaden project scope
- Invent commands and present them as confirmed facts

## File-specific rules

`.ai/project.yaml`: keep values structured and compact. Update only with repo evidence. Keep assumptions explicit.

`.ai/memory.md`: append short operational facts. Remove stale items when disproven. Do not turn it into a changelog.

`.ai/decisions.md`: record only stable decisions. Each must include why it exists. No temporary debugging choices.

Local subdirectory AGENTS.md: keep narrower than root AGENTS.md. Only add local constraints that truly help execution.

## Stop conditions

- Stop if evidence is too weak
- Stop if requested change would alter the immutable core
- Stop if maintenance would require product implementation
- Stop if multiple conflicting interpretations exist with no clear winner

If you stop, output: what is unclear, what evidence is missing, the smallest safe next step.

## Token budget

Maintenance output ≤80 lines.

## Output format

- Scope checked
- Files updated
- Confirmed changes
- Assumptions added / removed
- Unknowns remaining
- Core-change warning (if any)
