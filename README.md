# AI Dev Workflow Template

A plug-and-play scaffold for a disciplined multi-agent coding workflow.

Role assignments are configured in `.ai/models.yaml`. Default: plan=`claude/sonnet-4-6`, execute=`codex/gpt-5.4`, review=`claude/opus-4-6`.

The orchestrator must dispatch each phase through the configured tool and model. These assignments are runtime controls, not hints.

## What this gives you

- Task triage by size (`trivial` / `small` / `medium` / `large`) to avoid over-engineering
- Structured handoff packets for `plan -> execute -> review -> rescue`
- Token budget constraints on every skill output
- Bootstrap prerequisite guard so skills do not run on empty project metadata
- Post-execution handoff that feeds directly into review
- Memory contract for persisting operational discoveries
- Reusable Claude skills under `.claude/skills/`

## Folder layout

```text
.
|-- AGENTS.md                      # Codex-facing execution rules (managed block)
|-- CLAUDE.md                      # Claude import (managed block)
|-- .ai/
|   |-- project.yaml              # Project metadata (filled by bootstrap)
|   |-- memory.md                 # Operational facts
|   |-- decisions.md              # Stable architecture decisions
|   |-- models.yaml               # Tool/model assignment per workflow phase
|   |-- packets/
|   |   |-- plan.md               # Planning packet schema
|   |   |-- execute.md            # Execution packet schema (with Steps + Handoff)
|   |   |-- review.md             # Review packet schema (with checklist)
|   |   `-- rescue.md             # Rescue packet schema
|   `-- workflow/
|       |-- agents-block.md       # Injected into AGENTS.md
|       `-- claude-workflow.md    # Pipeline contract + shared rules
`-- .claude/
    `-- skills/
        |-- bootstrap/SKILL.md
        |-- planner/SKILL.md
        |-- reviewer/SKILL.md
        |-- maintenance/SKILL.md
        `-- rescue/SKILL.md
```

## Model configuration

Model assignments live in `.ai/models.yaml`. Each phase has a `tool` (`claude` or `codex`) and a `model` field.

```yaml
plan:
  tool: claude
  model: claude-sonnet-4-6

execute:
  tool: codex
  model: gpt-5.4
```

Default assignments:

| Phase | Tool | Model |
|-------|------|-------|
| plan | claude | claude-sonnet-4-6 |
| execute | codex | gpt-5.4 |
| review | claude | claude-opus-4-6 |
| rescue | claude | claude-opus-4-6 |
| maintenance | claude | claude-sonnet-4-6 |
| bootstrap | claude | claude-sonnet-4-6 |

Edit any field to swap models or tools. `install.sh` copies this file as `copy_if_missing` so customizations survive re-runs.

When you run the orchestrate skill, it should read `.ai/models.yaml` and launch each phase with the configured tool and model explicitly. Example: if orchestration starts in Sonnet but `plan.model` is `claude-opus-4-6`, the orchestrator should spawn Opus for planning instead of planning in the starter session.

## Setup

### 1. Install the scaffold

From the target repo root:

```bash
bash /path/to/ai-dev-workflow-template/install.sh .
```

This copies the workflow files into your repo, creates `AGENTS.md` and `CLAUDE.md` managed blocks, and preserves any existing instructions in those files.

### 2. Bootstrap the project

To preserve strict phase-to-model matching, launch bootstrap through the tool and model assigned to `bootstrap` in `.ai/models.yaml`, then run:

```text
Use the bootstrap skill. Adapt this repository to the workflow scaffold.
```

Bootstrap detects the stack, commands, directories, and boundaries, fills `.ai/project.yaml`, and may create subdirectory `AGENTS.md` files when the repo has clear domain separation.

## Task flow

### Pipeline

1. **Triage**: planner classifies size (`trivial`, `small`, `medium`, `large`)
2. **Plan**: planner produces execution packet(s)
3. **Execute**: executor follows steps and fills the Handoff section
4. **Review**: reviewer checks Handoff output (skip for trivial, optional for safe small tasks)
5. **Maintain**: update `memory.md` and `decisions.md` with discoveries

### Size gate

| Size | Scope | Pipeline |
|------|-------|----------|
| trivial | single file, <10 lines | one-line instruction, no packets |
| small | 1-3 files, clear scope | minimal execution packet, review only if risky |
| medium | 4-10 files or cross-subsystem | full plan + execute + review |
| large | >10 files or unclear architecture | full plan + execute + review |

### Running a task (full pipeline)

Invoke the orchestrate skill with your task. The starter session acts as a controller and dispatches each phase to the configured tool/model automatically:

```text
Use the orchestrate skill.

Task: [describe the task]
```

The orchestrator will:
1. Triage the task size
2. Dispatch planning to the tool/model configured for `plan`
3. Send the execution packet to the tool/model configured for `execute`
4. Dispatch review to the tool/model configured for `review` if needed
5. Ask you before sending changes back to Codex if issues are found
6. Dispatch maintenance to the tool/model configured for `maintenance` if memory updates are needed
7. Report the outcome, including which tool/model ran each phase

### Running phases manually

If you want to control each phase individually, start that phase in the tool/model assigned in `.ai/models.yaml`. Manual runs are only guaranteed to match the configured model when launched that way.

**Plan only:**
```text
Use the planner skill.
Task: [describe the task]
```

**Review only** (paste the Handoff from Codex):
```text
Use the reviewer skill.
[paste Handoff here]
```

**Recovery after failure:**
```text
Use the rescue skill.
Task: [original task]
What was attempted: [summary]
What failed: [evidence]
```

## Operating principles

- Keep packets narrow
- Stop on blockers instead of guessing
- List assumptions explicitly
- Do not silently broaden scope
- Fill the Handoff section before declaring done
- Update `memory.md` after tasks that reveal operational facts

## Maintenance

After successful tasks or repo changes:

```text
Use the maintenance skill.
```

This refreshes `.ai/project.yaml`, `.ai/memory.md`, and `.ai/decisions.md` based on current repo state.

## Notes

- `AGENTS.md` is the main Codex-facing instruction surface.
- `.claude/skills/*` contains reusable Claude behaviors.
- `.ai/project.yaml` is the mutable adapter layer per project.
- `.ai/packets/*` are schemas; the skills fill the content.
