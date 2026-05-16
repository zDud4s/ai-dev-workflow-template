# AI workflow shared instructions

## Pipeline

1. **Triage**: planner classifies task size (trivial / small / medium / large)
2. **Plan**: planner produces execution packet(s) using `.ai/packets/` schemas
3. **Execute**: executor follows packet steps literally, fills Handoff section when done
4. **Review**: reviewer checks Handoff output (skip for trivial; optional for small unless risky)
5. **Maintain**: update `.ai/memory.md` and `.ai/decisions.md` with discoveries

## Roles

Role assignments are configured in `.ai/models.yaml`.
Default: plan=claude/sonnet-4-6, execute=codex/o4-mini, review=claude/opus-4-6

The orchestrator is a controller only. It must dispatch each phase through the tool and model configured in `.ai/models.yaml` and must not substitute the current session model for plan, review, rescue, maintenance, or bootstrap.

## Dispatch

The shared dispatch mechanism (routing modes, prompt-passing, resume rule, config error table) lives in `.ai/workflow/dispatch.md`. Any controller — orchestrator today, others later — reads it once and follows it.

## Layer model

The repo has three layers. Knowing which one a file belongs to tells you whether it is read-only, mutable per-task, or mutable per-run.

| Layer | Files | Mutability |
|---|---|---|
| **Workflow core** | `.ai/workflow/*.md`, `.claude/skills/*/SKILL.md`, `install.sh`, scripts | Read-only during normal task runs. Only changes when evolving the workflow itself. |
| **Packet schemas** | `.ai/packets/*.md` | **Read-only templates.** Phases READ them to learn the format and EMIT filled copies in their own output (or to temp files for dispatch). NEVER edit these files during a normal task. |
| **Project state** | `.ai/project.yaml`, `.ai/memory.md`, `.ai/decisions.md` | Mutable per task. The `maintenance` phase appends to memory; bootstrap and human edits update the rest. |
| **Task instances** | `.ai/plans/<date>-<slug>.md`, `.ai/specs/<date>-<slug>.md` | Optional persistent copies of filled plans/specs for medium/large tasks. New files only — never overwrite existing dated files. |

Filled packets flow ephemerally between phases via stdin/temp files (see `dispatch.md`). They are NOT written back into `.ai/packets/`. If you ever find yourself running `Edit` or `Write` against `.ai/packets/*.md` during a task, STOP — that's a workflow violation.

## Rules

1. If `project_name` in `.ai/project.yaml` is `unknown`, run bootstrap first.
2. Preserve existing repository instructions.
3. Do not rewrite the workflow core during bootstrap.
4. Do not implement product changes during bootstrap.
5. Use `.ai/project.yaml`, `.ai/memory.md`, and `.ai/decisions.md` as the mutable project layer.
6. Executor must fill the Handoff section of the execution packet before declaring done. `Validation evidence` is mandatory — one block per validation command with exit code and output tail. Self-reported success without evidence is not acceptable.
7. Prefer the smallest correct change.
8. Do not broaden scope silently.
9. A phase only counts as correctly executed if it was launched through the configured tool/model for that phase.
10. Review is gated by two independent signals: **Risk level** (planner computes from `boundaries.*`) and **Size**. Review runs when Risk level is `elevated` OR Size is `medium`/`large`. Size alone never bypasses risk.
