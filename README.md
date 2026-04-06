# AI Dev Workflow Template

A repo scaffold for a disciplined multi-agent coding workflow:

- **Sonnet** = planning, decomposition, repo adaptation, and task routing
- **Codex** = scoped implementation
- **Opus** = escalation, architecture review, and failure analysis

The design goal is to make setup as close as possible to:

1. copy this scaffold into a repository
2. ask Sonnet to adapt it to the project

## What this gives you

- repo-persistent operating rules via `AGENTS.md`
- project-specific facts in `.ai/project.yaml`
- compact task packets for plan / execute / review / rescue
- reusable Claude skills under `.claude/skills/`
- optional subdirectory `AGENTS.md` files for backend / frontend / infra overrides

## Folder layout

```text
.
├─ AGENTS.md
├─ CLAUDE.md
├─ .ai/
│  ├─ project.yaml
│  ├─ memory.md
│  ├─ decisions.md
│  ├─ packets/
│  │  ├─ plan.md
│  │  ├─ execute.md
│  │  ├─ review.md
│  │  └─ rescue.md
│  └─ templates/
│     ├─ bootstrap-prompt.md
│     ├─ task-prompt.md
│     ├─ review-prompt.md
│     └─ recovery-prompt.md
├─ .claude/
│  └─ skills/
│     ├─ bootstrap/SKILL.md
│     ├─ planner/SKILL.md
│     ├─ reviewer/SKILL.md
│     └─ rescue/SKILL.md
├─ backend/AGENTS.md
├─ frontend/AGENTS.md
└─ infra/AGENTS.md
```

## Fast start

### Option A: copy the scaffold into the repo

From the target repo root:

```bash
cp -r /path/to/ai-dev-workflow-template/.ai .
cp -r /path/to/ai-dev-workflow-template/.claude .
cp /path/to/ai-dev-workflow-template/AGENTS.md .
cp /path/to/ai-dev-workflow-template/CLAUDE.md .
```

### Option B: keep it as a separate template repo and copy what you need

The template is intentionally flat and portable.

## Bootstrap the project

Open Claude Sonnet in the target repo and paste the contents of `.ai/templates/bootstrap-prompt.md`, or simply use:

```text
Use the bootstrap skill.

Adapt this repository to the workflow scaffold without implementing product changes.

Required outputs:
1. Fill `.ai/project.yaml`
2. Tighten project-specific instructions in `AGENTS.md` and `CLAUDE.md` only where needed
3. Record discovered quirks in `.ai/memory.md`
4. List assumptions and unknowns explicitly

Constraints:
- Preserve the fixed workflow roles
- Do not invent commands without marking them as assumptions
- Do not broaden scope into product work
```

## Normal task flow

### 1) Planning in Sonnet

Use `.ai/templates/task-prompt.md` and provide the task.

### 2) Execution in Codex

Give Codex the execution packet from `.ai/packets/execute.md`.

### 3) Review in Opus

Use `.ai/templates/review-prompt.md` for risky, cross-cutting, or failed work.

### 4) Recovery after failure

Use `.ai/templates/recovery-prompt.md` after repeated failure or implementation drift.

## Operating principles

- keep packets narrow
- stop on blockers instead of guessing
- list assumptions explicitly
- do not silently broaden scope
- do not let the bootstrap pass start product work

## Recommended maintenance

After a successful task:

- add stable discoveries to `.ai/memory.md`
- record real architectural choices in `.ai/decisions.md`
- tighten `AGENTS.md` only when a rule should truly persist

## Notes

- `AGENTS.md` is the main Codex-facing repo instruction surface.
- `.claude/skills/*` contains reusable Claude behaviors.
- `.ai/project.yaml` is the mutable adapter layer per project.
- `backend/AGENTS.md`, `frontend/AGENTS.md`, and `infra/AGENTS.md` are optional local overrides.
