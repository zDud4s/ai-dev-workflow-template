# AI workflow shared instructions

## Pipeline

1. **Triage**: planner classifies task size (trivial / small / medium / large)
2. **Plan**: planner produces execution packet(s) using `.ai/packets/` schemas
3. **Execute**: executor follows packet steps literally, fills Handoff section when done
4. **Review**: reviewer checks Handoff output (runs when Risk is `elevated` or Size is `medium`/`large` — see Rule 6)
5. **Maintain**: update `.ai/memory.md` and `.ai/decisions.md` with discoveries

For non-code task orchestration through the user's agent catalog, use the sibling entry point at `.claude/skills/orchestrate-agents/`.

## Roles

Role assignments live in `.ai/models.yaml`. The orchestrator is a controller only — it dispatches each phase through the configured tool/model and never substitutes its own model for a configured phase.

## Dispatch

The shared dispatch mechanism (routing modes, prompt-passing, resume rule, config error table) lives in `.ai/workflow/dispatch.md`. Any controller — orchestrator today, others later — reads it once and follows it.

## Layer model

| Layer | Files | Mutability |
|---|---|---|
| **Workflow core** | `.ai/workflow/*.md`, `.claude/skills/*/SKILL.md`, `.agents/skills/*/SKILL.md` (mirror, regenerated from `.claude/skills/`), install scripts | Read-only — changes only when evolving the workflow. |
| **Packet schemas** | `.ai/packets/*.md` | **Read-only templates.** Phases READ + EMIT filled copies; never edit. |
| **Project state** | `.ai/project.yaml`, `.ai/memory.md`, `.ai/decisions.md` (+ `.ai/memory-archive.md`, append-only by consolidation, never read by phases) | Mutable per task by `maintenance` + human edits. |
| **Task instances** | `.ai/plans/<date>-<slug>.md`, `.ai/specs/<date>-<slug>.md` | New-file persistent copies for medium/large tasks; never overwrite. |

Filled packets flow via stdin/temp files (see `dispatch.md`); never Edit/Write `.ai/packets/*.md` during a task — workflow violation.

### Directory reference

Sibling `.ai/` directories with overlapping names, disambiguated by actual use:

| Directory | Holds | Distinct from sibling |
|---|---|---|
| `.ai/plans/` | Persisted execution plan (packet-level breakdown) for medium/large code tasks. | `specs/` is the design; `plans/` is the executable phase breakdown. |
| `.ai/specs/` | Persisted spec (broader design/intent) for large tasks. | Written before/above the plan; large tasks may have both. |
| `.ai/pipelines/` | Saved agent-pipeline YAML definitions (structure-only DAGs) authored by `orchestrate-agents`. | Definitions, not results — per-developer, gitignored. |
| `.ai/agent-runs/` | Filled run records persisted by `run-pipeline` after executing a pipeline. | Outputs of a pipeline run, not its definition. |
| `.ai/ledgers/` | Durable append-only JSONL: `metrics.jsonl`, `events.jsonl`, `jobs.jsonl` (snapshot index), `todos.jsonl`. | Permanent observability log; survives across jobs. |
| `.ai/dashboard/jobs/` | Per-job runtime files `<id>.log` / `<id>.json` from the dashboard. | Transient working files (pruned after 7 days), not the durable ledger. |

## Rules

1. If `project_name` in `.ai/project.yaml` is `unknown`, run bootstrap first.
2. Bootstrap may NOT rewrite the workflow core nor implement product changes; preserve existing repository instructions.
3. Executor must fill the Handoff section before declaring done. `Validation evidence` is mandatory — one block per validation command (exit code + output tail). Self-reported success without evidence is not acceptable.
4. Prefer the smallest correct change; do not broaden scope silently.
5. A phase SHOULD be launched through the tool/model configured in `.ai/models.yaml` (or the planner's `## Selected models` block when auto-select is enabled). Manual runs that bypass dispatch don't generate a `.ai/ledgers/metrics.jsonl` row, so they can't be scored by the adaptive selector and won't appear in `## Phase execution log` — the orchestrator surfaces them as `source=manual` if a user later replays them through the pipeline.
6. Review runs when Risk level is `elevated` OR Size is `medium`/`large`. Size alone never bypasses risk; for code changes the deterministic gate is the ship/no-ship decision; LLM review is advisory.
