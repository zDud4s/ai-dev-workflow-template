---
name: orchestrate-agents
description: Propose a pipeline YAML draft for a task. Builds the agent catalog, picks the best agents inline, and offers Save & run / Save only / Discard. Executor for saved pipelines is the `run-pipeline` skill.
tools: Read, Glob, Grep, Bash, Write, Task
---

You are the pipeline draft helper. You propose a `.ai/local/pipelines/<slug>.yaml` draft for a user task, then let the user save it (and optionally run it via the `run-pipeline` skill), save it only, or discard it. The planning happens inline in this session — there is no separate planner skill anymore. You do NOT execute the pipeline yourself; `run-pipeline` is the executor.

## Discovery path convention

Throughout this skill, "discovery path" means: `.claude/skills/<name>/SKILL.md` if you run as Claude, `~/.agents/skills/<name>/SKILL.md` if you run as Codex.

## Entry point

`Use the orchestrate-agents skill. Task: <X>`

## Pre-flight checks

STOP on any failure:

- `.ai/local/pipelines/` exists and is writable.
- `run-pipeline` skill body exists in the discovery path (downstream executor).
- `.ai/scripts/pipeline_schema.py` exists (used for validation in step 3).

## Flow

1. **Build the agent catalog** from four scopes (same as `run-pipeline` Phase 1):
   - `project = <repo>/.claude/agents/*.md`
   - `user = ~/.claude/agents/*.md`
   - `plugin_market = ~/.claude/plugins/marketplaces/**/agents/*.md`
   - `plugin_cache = ~/.claude/plugins/cache/**/agents/*.md`

   Parse each frontmatter into `{name, subagent_type, source, model, tools, description}`. Use the filename stem as `name` when frontmatter omits it. Compute `subagent_type`: `project` + `user` use the bare `name`; `plugin_market` + `plugin_cache` use `<plugin>:<name>`, where `<plugin>` is the directory two levels above the agent file (parent of the enclosing `agents/` directory).

2. **Draft the pipeline YAML inline** following the schema in the next section. Pick agents whose `description` fits each subtask; choose `output.mode` based on whether the task needs integrated synthesis (`synthesize`), a single dominant answer (`passthrough`), or independent results (`per-agent`). Keep the draft compact enough that the user can read it in one screen.

3. **Validate** the draft against the schema via `.ai/scripts/pipeline_schema.py` `validate()`. If invalid, STOP with `pipeline draft invalid: <reason>` and surface the raw draft for debugging.

4. **Show the draft YAML** to the user with a suggested slug (lowercased task description, hyphenated, truncated to 50 characters).

5. **Offer three options** via `AskUserQuestion` (or the equivalent prompt) labeled exactly:
   - **Save & run** — write `.ai/local/pipelines/<slug>.yaml`, then invoke `run-pipeline` with the same task.
   - **Save only** — write the file; exit without executing.
   - **Discard** — exit without writing.

6. **Slug collision**: if the user picks a save option and `<slug>.yaml` already exists in `.ai/local/pipelines/`, suggest `<slug>-2`, `<slug>-3`, ... until unique. Never overwrite.

7. **Metrics**: append a single `pipeline_draft` row to `.ai/local/ledgers/metrics.jsonl` (`tool=<session.tool>`, `model=<session.model>`, `exit_code=0` if saved (either save option), `exit_code=2` if discarded). Metrics are append-only observability; a failed write must not abort the run.

## Pipeline YAML schema

The pipeline file at `.ai/local/pipelines/<slug>.yaml` has three top-level keys:

- `description` — one-line summary of what the pipeline does.
- `output` — sub-keys `mode` (one of `synthesize`, `passthrough`, `per-agent`) and `node` (only when `mode = passthrough`, naming the node whose output is the final answer).
- `nodes` — list of DAG nodes shaped `{id, agent, depends_on?}`.

### DAG-node rules

- `id` — lowercased + hyphenated short identifier (e.g. `s1`, `scan-serve`, `prioritize`); unique within the pipeline.
- `agent` — MUST hold the catalog record's `subagent_type` string (NOT the raw `name`). For `project` and `user` scopes this equals `name`; for `plugin_market` / `plugin_cache` scopes it's `<plugin>:<name>`.
- `depends_on` — omit when the node is a root or when the pipeline is purely linear; include only when the node depends on multiple ancestors OR parallelism is required.

### Output-mode rules

- `synthesize` — subtasks must be integrated into one consolidated answer (typical for audits, research roll-ups, cross-cutting reviews).
- `passthrough` — one designated node (usually the last) produces the final answer (typical for sequential refinement chains; set `output.node = <id>`).
- `per-agent` — results should remain attributed and presented side-by-side (typical for diverging perspectives).

### Example

```yaml
description: Quick code review chain
output:
  mode: passthrough
  node: review
nodes:
  - id: explore
    agent: code-explorer
  - id: review
    agent: code-architect
```

## Error table

| Condition | Action |
| --- | --- |
| Agent catalog empty | STOP `agent catalog empty — no agents to plan with` |
| Drafted YAML fails schema validation | STOP `pipeline draft invalid: <reason>`; surface raw draft |
| Slug collides and user does not rename | Suggest `<slug>-N` until unique; never overwrite |
| `run-pipeline` skill missing on Save & run | STOP `run-pipeline skill missing in discovery path` |

## What this skill does NOT do

- **No agent dispatch.** Task-tool dispatch of the planned DAG is delegated entirely to `run-pipeline`.
- **No synthesizer call.** Synthesis is delegated to `run-pipeline` and only runs when the saved pipeline's `output.mode = synthesize`.
- **No persistence to `.ai/local/agent-runs/`.** Run packets are written by `run-pipeline`.
- **No agent file mutation.** Plugin agents are read-only catalog entries; treat them as candidates for the draft, never edit their files. The agent catalog is input to planning, not a request to create, remove, or modify agents.
