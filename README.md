# AI Dev Workflow Template

A plug-and-play scaffold for a disciplined multi-agent coding workflow.

Role assignments are configured in `.ai/models.yaml`. Default: plan=`claude/opus-4-7`, execute=`codex/gpt-5.5`, review=`claude/opus-4-7`.

The orchestrator must dispatch each phase through the configured tool and model. These assignments are runtime controls, not hints. Dispatch routing is contracted in `.ai/workflow/dispatch.md` and toggled with `dispatch_mode: auto | manual` in `.ai/models.yaml`.

## What this gives you

- Task triage by size (`trivial` / `small` / `medium` / `large`) to avoid over-engineering
- Structured handoff packets for `plan -> execute -> review -> rescue`
- Token budget constraints on every skill output
- Bootstrap prerequisite guard so skills do not run on empty project metadata
- Post-execution handoff that feeds directly into review
- Memory contract for persisting operational discoveries
- Reusable skills under `.claude/skills/` (canonical) mirrored to `.agents/skills/` so Codex sees the same set
- Single dispatch contract (`.ai/workflow/dispatch.md`) shared by every controller
- Persistent plan/spec history under `.ai/plans/` and `.ai/specs/`
- Local web dashboard for tailing events, browsing plans/specs, and spawning orchestrate/planner jobs

## Folder layout

```text
.
|-- AGENTS.md                      # Codex-facing execution rules (managed block)
|-- CLAUDE.md                      # Claude import (managed block)
|-- .ai/
|   |-- project.yaml              # Project metadata (filled by bootstrap)
|   |-- memory.md                 # Operational facts
|   |-- decisions.md              # Stable architecture decisions
|   |-- models.yaml               # Tool/model assignment + dispatch_mode per phase
|   |-- events.jsonl              # Append-only telemetry stream (gitignored)
|   |-- plans/                    # Persistent filled plans for medium/large tasks
|   |-- specs/                    # Persistent spec documents
|   |-- dashboard/
|   |   |-- index.html            # Local web UI (memory, decisions, jobs, events)
|   |   |-- styles.css            # Dashboard stylesheet
|   |   |-- app/                  # Split frontend modules
|   |   |   |-- core.js           # Helpers, nav, fetch, renderers, forms
|   |   |   |-- skills.js         # Skills catalog, detail modal, proposals
|   |   |   |-- jobs.js           # Jobs, events, timeline, dispatch toggle
|   |   |   |-- terminals.js      # Multi-pane terminals + IDE transcripts
|   |   |   `-- main.js           # loadAll() bootstrap
|   |   |-- serve.py              # Static + JSON API server
|   |   |-- log_event.py          # PostToolUse hook -> events.jsonl
|   |   `-- jobs/                 # Per-job log dirs (gitignored)
|   |-- packets/
|   |   |-- plan.md               # Planning packet schema
|   |   |-- execute.md            # Execution packet schema (with Steps + Handoff)
|   |   |-- review.md             # Review packet schema (with checklist)
|   |   `-- rescue.md             # Rescue packet schema
|   `-- workflow/
|       |-- agents-block.md       # Injected into AGENTS.md
|       |-- workflow.md           # Pipeline contract + shared rules
|       `-- dispatch.md           # Routing modes, prompt-passing, resume rule
|-- .claude/
|   `-- skills/                   # Canonical source for shared skills
|       |-- orchestrate/SKILL.md
|       |-- planner/SKILL.md
|       |-- reviewer/SKILL.md
|       |-- maintenance/SKILL.md
|       |-- rescue/SKILL.md
|       |-- bootstrap/SKILL.md
|       `-- codex/SKILL.md        # Claude-side: how to call Codex
`-- .agents/
    `-- skills/                   # Codex-visible mirror (synced from .claude/skills/)
        |-- claude/SKILL.md       # Codex-only: how to call Claude (no Claude counterpart)
        `-- <shared skills>       # Mirrored copies of .claude/skills/* — edit upstream
```

## Model configuration

Model assignments live in `.ai/models.yaml`. Each phase has a `tool` (`claude` or `codex`) and a `model` field. Optional per-phase fields: `mode` (`inline` | `agent` | `dispatcher`), `timeout_seconds`, and `reasoning_effort` (claude accepts `{low, medium, high, xhigh, max}` via `--effort`; codex accepts `{low, medium, high, xhigh}` via `--config model_reasoning_effort`; `max` is claude-only).

```yaml
dispatch_mode: auto    # auto | manual

session:
  tool: claude
  model: claude-sonnet-4-6

plan:
  tool: claude
  model: claude-opus-4-7

execute:
  tool: codex
  model: gpt-5.5
```

Default assignments:

| Phase | Tool | Model |
|-------|------|-------|
| session | claude | claude-sonnet-4-6 |
| plan | claude | claude-opus-4-7 |
| execute | codex | gpt-5.5 |
| review | claude | claude-opus-4-7 |
| rescue | claude | claude-opus-4-6 |
| maintenance | claude | claude-sonnet-4-6 |
| bootstrap | claude | claude-sonnet-4-6 |

Available models (refreshed May 2026):
- Claude: `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`
- Codex: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`

Edit any field to swap models or tools. `install.sh` copies this file as `copy_if_missing` so customizations survive re-runs.

When you run the orchestrate skill, it reads `.ai/models.yaml` and launches each phase with the configured tool and model explicitly. Example: if orchestration starts in Sonnet but `plan.model` is `claude-opus-4-6`, the orchestrator spawns Opus for planning instead of planning in the starter session. The full routing contract (auto vs. manual dispatch, prompt-passing, resume rule, error table) lives in `.ai/workflow/dispatch.md`.

## Setup

### 1. Install the scaffold

From the target repo root:

```bash
bash /path/to/ai-dev-workflow-template/install.sh .
```

This copies the workflow files into your repo, creates `AGENTS.md` and `CLAUDE.md` managed blocks, and preserves any existing instructions in those files.

### 1.1 Update an existing project

If you changed a shared workflow skill and want to propagate that update to a project that already uses this scaffold, run:

```bash
bash /path/to/ai-dev-workflow-template/update-workflow.sh /path/to/target-project
```

By default this updates:
- `.claude/skills/*` (canonical source)
- `.agents/skills/{orchestrate,planner,reviewer,maintenance,rescue,bootstrap,codex,claude}/SKILL.md` (in-repo Codex mirror)
- `~/.agents/skills/{orchestrate,planner,reviewer,maintenance,rescue,bootstrap,codex,claude}/SKILL.md` (global Codex mirror)
- `.ai/workflow/*` (`workflow.md`, `dispatch.md`, `agents-block.md`)
- managed blocks in `AGENTS.md` and `CLAUDE.md`

By default this preserves:
- `.ai/packets/*`
- `.ai/models.yaml`
- `.ai/project.yaml`
- `.ai/memory.md`
- `.ai/decisions.md`

If you also want to refresh packet templates:

```bash
bash /path/to/ai-dev-workflow-template/update-workflow.sh /path/to/target-project --include-packets
```

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

## Dashboard

A local web dashboard lives under `.ai/dashboard/`. From the repo root:

```bash
python .ai/dashboard/serve.py
```

Then open <http://localhost:8765/.ai/dashboard/>. It serves the repo as read-only static files and exposes a small JSON API:

- Browse `.ai/plans/` and `.ai/specs/`
- Append entries to `memory.md` and `decisions.md` from the UI
- Tail `.ai/events.jsonl` in real time and clear it
- Flip `dispatch_mode` between `auto` and `manual`
- Spawn orchestrate / planner subprocesses, stream their logs, and write to stdin

Events and per-job logs (`.ai/events.jsonl`, `.ai/dashboard/jobs/`) are gitignored.

## Notes

- `AGENTS.md` is the main Codex-facing instruction surface; `CLAUDE.md` imports `.ai/workflow/workflow.md`.
- `.claude/skills/*` is the canonical source for shared skills. `install.sh` mirrors them into `.agents/skills/` (in-repo) and `~/.agents/skills/` (global) so Codex can discover the same set — edit upstream, not in the mirrors.
- `.ai/project.yaml` is the mutable adapter layer per project.
- `.ai/packets/*` are read-only schemas; phases READ them and EMIT filled copies through stdin/temp files. Never edit packet files during a task.
- `.ai/plans/<date>-<slug>.md` and `.ai/specs/<date>-<slug>.md` are optional persistent copies for medium/large tasks. New files only — never overwrite existing dated files.
