# AI workflow shared instructions

## Pipeline

1. **Triage**: planner classifies task size (trivial / small / medium / large)
2. **Plan**: planner produces execution packet(s) using `.ai/packets/` schemas
3. **Execute**: executor follows packet steps literally, fills Handoff section when done
4. **Review**: reviewer checks Handoff output (skip for trivial; optional for small unless risky)
5. **Maintain**: update `.ai/memory.md` and `.ai/decisions.md` with discoveries

## Roles

Role assignments live in `.ai/models.yaml`. The orchestrator is a controller only — it dispatches each phase through the configured tool/model and never substitutes its own model for a configured phase.

## Dispatch

The shared dispatch mechanism (routing modes, prompt-passing, resume rule, config error table) lives in `.ai/workflow/dispatch.md`. Any controller — orchestrator today, others later — reads it once and follows it.

## Layer model

| Layer | Files | Mutability |
|---|---|---|
| **Workflow core** | `.ai/workflow/*.md`, `.claude/skills/*/SKILL.md`, install scripts | Read-only — changes only when evolving the workflow. |
| **Packet schemas** | `.ai/packets/*.md` | **Read-only templates.** Phases READ + EMIT filled copies; never edit. |
| **Project state** | `.ai/project.yaml`, `.ai/memory.md`, `.ai/decisions.md` (+ `.ai/memory-archive.md`, append-only by consolidation, never read by phases) | Mutable per task by `maintenance` + human edits. |
| **Task instances** | `.ai/plans/<date>-<slug>.md`, `.ai/specs/<date>-<slug>.md` | New-file persistent copies for medium/large tasks; never overwrite. |

Filled packets flow via stdin/temp files (see `dispatch.md`); never Edit/Write `.ai/packets/*.md` during a task — workflow violation.

## Rules

1. If `project_name` in `.ai/project.yaml` is `unknown`, run bootstrap first.
2. Bootstrap may NOT rewrite the workflow core nor implement product changes; preserve existing repository instructions.
3. Executor must fill the Handoff section before declaring done. `Validation evidence` is mandatory — one block per validation command (exit code + output tail). Self-reported success without evidence is not acceptable.
4. Prefer the smallest correct change; do not broaden scope silently.
5. A phase only counts as correctly executed if launched through the tool/model configured in `.ai/models.yaml`.
6. Review runs when Risk level is `elevated` OR Size is `medium`/`large`. Size alone never bypasses risk.
