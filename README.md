# AI Dev Workflow Template

A plug-and-play scaffold for a disciplined multi-agent coding workflow:

- **Sonnet** = planning, triage, decomposition, repo adaptation
- **Codex** = scoped implementation (follows execution packets literally)
- **Opus** = escalation, architecture review, and failure analysis

## What this gives you

- Task triage by size (trivial / small / medium / large) to avoid over-engineering
- Structured handoff packets for plan → execute → review → rescue
- Token budget constraints on every skill output
- Bootstrap prerequisite guard (no skill runs on empty project metadata)
- Post-execution handoff that feeds directly into review
- Memory contract for persisting operational discoveries
- Reusable Claude skills under `.claude/skills/`

## Folder layout

```text
.
├─ AGENTS.md                      # Codex-facing execution rules (managed block)
├─ CLAUDE.md                      # Claude import (managed block)
├─ .ai/
│  ├─ project.yaml                # Project metadata (filled by bootstrap)
│  ├─ memory.md                   # Operational facts
│  ├─ decisions.md                # Stable architecture decisions
│  ├─ packets/
│  │  ├─ plan.md                  # Planning packet schema
│  │  ├─ execute.md               # Execution packet schema (with Steps + Handoff)
│  │  ├─ review.md                # Review packet schema (with checklist)
│  │  └─ rescue.md                # Rescue packet schema
│  └─ workflow/
│     ├─ agents-block.md          # Injected into AGENTS.md
│     └─ claude-workflow.md       # Pipeline contract + shared rules
└─ .claude/
   └─ skills/
      ├─ bootstrap/SKILL.md       # Onboard a repo to the workflow
      ├─ planner/SKILL.md         # Triage + plan + produce packets
      ├─ reviewer/SKILL.md        # Review execution output
      ├─ maintenance/SKILL.md     # Refresh project metadata
      └─ rescue/SKILL.md          # Recover from failed attempts
```

## Setup

### 1. Install the scaffold

From the target repo root:

```bash
bash /path/to/ai-dev-workflow-template/install.sh .
```

This copies the workflow files into your repo, creates `AGENTS.md` and `CLAUDE.md` managed blocks, and preserves any existing instructions in those files.

### 2. Bootstrap the project

Open Claude (Sonnet) in the target repo and run:

```text
Use the bootstrap skill. Adapt this repository to the workflow scaffold.
```

Bootstrap will detect the stack, commands, directories, boundaries, and fill `.ai/project.yaml`. It will also create subdirectory AGENTS.md files if the project has clear domain separation.

## Task flow

### Pipeline

1. **Triage** — planner classifies size: trivial, small, medium, large
2. **Plan** — planner produces execution packet(s)
3. **Execute** — executor follows steps, fills Handoff section
4. **Review** — reviewer checks Handoff (skip for trivial/safe-small)
5. **Maintain** — update memory.md and decisions.md with discoveries

### Size gate

| Size | Scope | Pipeline |
|------|-------|----------|
| trivial | single file, <10 lines | one-line instruction, no packets |
| small | 1-3 files, clear scope | minimal execution packet, review only if risky |
| medium | 4-10 files or cross-subsystem | full plan + execute + review |
| large | >10 files or unclear architecture | full plan + execute + Opus review |

### Running a task

Invoke the planner skill directly with your task:

```text
Use the planner skill.

Task: [describe the task]
```

The planner will triage, plan, and produce execution packet(s). Give the packet to Codex. After execution, the filled Handoff section feeds into review.

### Recovery after failure

```text
Use the rescue skill.

Task: [the original task]
What was attempted: [summary]
What failed: [evidence]
```

## Operating principles

- Keep packets narrow
- Stop on blockers instead of guessing
- List assumptions explicitly
- Do not silently broaden scope
- Fill the Handoff section before declaring done
- Update memory.md after tasks that reveal operational facts

## Maintenance

After successful tasks or repo changes:

```text
Use the maintenance skill.
```

This refreshes `.ai/project.yaml`, `.ai/memory.md`, and `.ai/decisions.md` based on current repo state.

## Notes

- `AGENTS.md` is the main Codex-facing instruction surface.
- `.claude/skills/*` contains reusable Claude behaviors (invoke directly, no templates needed).
- `.ai/project.yaml` is the mutable adapter layer per project.
- `.ai/packets/*` are schemas — they define the structure, skills fill the content.
