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

The repo has three layers. Knowing which one a file belongs to tells you whether it is read-only, mutable per-task, or mutable per-run.

| Layer | Files | Mutability |
|---|---|---|
| **Workflow core** | `.ai/workflow/*.md`, `.claude/skills/*/SKILL.md`, `install.sh`, scripts | Read-only during normal task runs. Only changes when evolving the workflow itself. |
| **Packet schemas** | `.ai/packets/*.md` | **Read-only templates.** Phases READ them to learn the format and EMIT filled copies in their own output (or to temp files for dispatch). NEVER edit these files during a normal task. |
| **Project state** | `.ai/project.yaml`, `.ai/memory.md`, `.ai/decisions.md` | Mutable per task. The `maintenance` phase appends to memory; bootstrap and human edits update the rest. |
| **Project state — historical** | `.ai/memory-archive.md` | Append-only by `maintenance` consolidation pass; never read by phases. Human reference. |
| **Task instances** | `.ai/plans/<date>-<slug>.md`, `.ai/specs/<date>-<slug>.md` | Optional persistent copies of filled plans/specs for medium/large tasks. New files only — never overwrite existing dated files. |

Filled packets flow ephemerally between phases via stdin/temp files (see `dispatch.md`). They are NOT written back into `.ai/packets/`. If you ever find yourself running `Edit` or `Write` against `.ai/packets/*.md` during a task, STOP — that's a workflow violation.

## Rules

1. If `project_name` in `.ai/project.yaml` is `unknown`, run bootstrap first.
2. Bootstrap may NOT rewrite the workflow core nor implement product changes; preserve existing repository instructions.
3. Executor must fill the Handoff section before declaring done. `Validation evidence` is mandatory — one block per validation command (exit code + output tail). Self-reported success without evidence is not acceptable.
4. Prefer the smallest correct change; do not broaden scope silently.
5. A phase only counts as correctly executed if launched through the tool/model configured in `.ai/models.yaml`.
6. Review runs when Risk level is `elevated` OR Size is `medium`/`large`. Size alone never bypasses risk.
