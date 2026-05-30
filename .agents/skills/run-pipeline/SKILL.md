---
name: run-pipeline
description: Execute a saved pipeline YAML from .ai/pipelines/<name>.yaml by dispatching its agents in-session via the Task tool.
tools: Read, Glob, Grep, Bash, Task, Write
---

You are the pipeline executor. You load a user-authored pipeline from `.ai/pipelines/<name>.yaml`, resolve every node against the live agent catalog, topo-dispatch the DAG with the Task tool, and apply the pipeline's declared output mode.

**Runtime branch.** Detect the runtime before dispatch. Claude sessions dispatch ready DAG layers through the `Task` tool (Phase 2A below). Codex sessions dispatch ready DAG layers through `codex exec` subprocesses orchestrated by `.ai/dashboard/scripts/pipeline_fanout.py` (Phase 2B below). Both paths reuse the same DAG resolution, output modes, and Wrap-up steps.

**Read `.ai/workflow/dispatch.md` once before starting.** It defines the dispatch contract used for the `synthesize` phase. Do not duplicate those rules here.

## Discovery path convention

Throughout this skill, "discovery path" means: `.claude/skills/<name>/SKILL.md` if you run as Claude, `~/.agents/skills/<name>/SKILL.md` if you run as Codex.

## Entry point

`Use the run-pipeline skill. Pipeline: <name>. Task: <description>`

## Pre-flight checks

STOP on any failure:

- `.ai/pipelines/<name>.yaml` exists.
- Slug matches `^[a-z0-9-]+$`.
- YAML parses (`yaml.safe_load`).
- Schema validation passes — reuse the dashboard validator (`from pipeline_schema import validate` in `.ai/dashboard/pipeline_schema.py`); surface every returned error verbatim.
- Only when pipeline `output.mode = synthesize`: `.ai/models.yaml` has `run_pipeline.synthesize` configured AND the `synthesizer` skill body exists in the discovery path. Other modes skip both checks.

## Phase 1 - Load + catalog

Build the agent catalog from four scopes:

- `project = <repo>/.claude/agents/*.md`
- `user = ~/.claude/agents/*.md`
- `plugin_market = ~/.claude/plugins/marketplaces/**/agents/*.md`
- `plugin_cache = ~/.claude/plugins/cache/**/agents/*.md`

For each discovered agent, parse frontmatter into `{name, subagent_type, source, model, tools, description}`. Use the filename stem as `name` when frontmatter omits it.

Compute `subagent_type` per scope:

- `project` + `user`: bare `name`.
- `plugin_market` + `plugin_cache`: `<plugin>:<name>`, where `<plugin>` is the directory two levels above the agent file (parent of the enclosing `agents/` directory). Example: `~/.claude/plugins/cache/<owner>/<plugin>/agents/foo.md` yields `<plugin>:foo`.

Resolve each pipeline `node.agent` string against the catalog's `subagent_type` values. STOP with `agent '<x>' not found in any scope at runtime` if any node is unresolvable.

## Phase 2A - Dispatch (Claude / Task fan-out)

Topo-sort the DAG. For each ready layer (every `depends_on` is `completed`), dispatch independent subtasks in a SINGLE assistant message with multiple Task calls (parallel). Dependent subtasks await their ancestors.

Prompt template (literal — the `2000` is the chosen runtime value):

```
ANCESTOR_OUTPUT_CHAR_LIMIT = 2000

prompt = """<original user task description>

You are <agent.description from catalog>.

<if has ancestors>
Relevant context from prior steps:
  - <ancestor.id> (<ancestor.agent>):
    <ancestor.output, truncated to ANCESTOR_OUTPUT_CHAR_LIMIT chars>
</if>

Apply your specialty to the task above. Return your specialized output.
"""

Task(subagent_type=node.agent, prompt=prompt)
```

`<node.agent>` is the computed `subagent_type` carried in the resolved DAG node, not a raw catalog `name`. There is no code-orchestrator escape hatch; Phase 2 never uses a subprocess.

Failure handling — three distinct node statuses:

| Status | When | Downstream |
| --- | --- | --- |
| `completed` | Task call returned non-empty output without error | Output flows to descendants |
| `failed` | Task call errored, refused, or returned empty | Descendants whose `depends_on` includes this node are marked `skipped` (not dispatched) |
| `skipped` | Any ancestor in the node's transitive `depends_on` is `failed` or `skipped` | Not dispatched; surfaced in output mode reporting |

Independent branches (no shared failed ancestor) continue normally. The pipeline halts only when no further progress is possible (every remaining node is `skipped`).

## Phase 2B - Dispatch (Codex / subprocess fan-out)

### Pre-flight (Codex-specific)

STOP with an explicit message naming the missing piece if any check fails:

- The `codex` binary is on PATH (`shutil.which("codex") is not None`).
- `.ai/models.yaml` has a top-level `run_pipeline.codex_dispatch` block with at least `model`, `reasoning_effort`, and `timeout_seconds`.
- Catalog source is restricted to project scope only: `<repo>/.claude/agents/*.md`. User-scope and plugin scopes are Claude-only and are NOT scanned in Codex sessions. Agent files are read as system-prompt prefixes for the dispatched subprocess.
- For each catalog agent, the effective Codex model is `<agent>.codex_model` from frontmatter when present, else `run_pipeline.codex_dispatch.model` from `.ai/models.yaml`.

### Dispatch

Topo-sort the DAG with the same readiness rules as Phase 2A. For each ready layer, build a JSON spec for `.ai/dashboard/scripts/pipeline_fanout.py` with one `node` per ready DAG node.

**Platform note (Windows):** `cmd[0]` must be the absolute path returned by `shutil.which("codex")` (e.g. `C:\Users\…\AppData\Roaming\npm\codex.CMD`). Python's `subprocess.run` does not consult `PATHEXT` when the first arg is a bare name, so the literal `"codex"` fails with `FileNotFoundError [WinError 2]` on Windows. Resolve once at pre-flight and reuse for every node. On POSIX, `shutil.which("codex")` returns the same string the shell would resolve, so the same pattern is portable.

```
{
  "nodes": [
    {
      "id": "<node.id>",
      "cmd": [
        "<shutil.which(\"codex\")>",
        "exec",
        "--skip-git-repo-check",
        "-m",
        "<resolved_model>",
        "--config",
        "model_reasoning_effort=<resolved_effort>",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        "<project_root>"
      ],
      "stdin": "<assembled_prompt_with_agent_body_+_ancestors>",
      "timeout": "<run_pipeline.codex_dispatch.timeout_seconds>"
    }
  ]
}
```

Invoke the helper synchronously. The helper itself already pins UTF-8 encoding when piping stdin to each node (Codex rejects non-UTF-8 input); callers do not need to set `encoding=` on this outer call.

```
subprocess.run(
    [sys.executable, ".ai/dashboard/scripts/pipeline_fanout.py"],
    input=json.dumps(spec),
    capture_output=True,
    text=True,
    encoding="utf-8",
)
```

Block until the helper exits. If it exits non-zero, STOP and report stderr. Otherwise parse stdout as JSON and map each per-node result:

| Helper result | DAG node status |
| --- | --- |
| `status=ok` | `completed` |
| `status=error` | `failed` |
| `status=timeout` | `failed` |

Descendants of failed nodes become `skipped`, using the same rule as Phase 2A. Independent branches continue normally.

### Metrics row spec (Codex dispatch)

For each dispatched node, append one compact JSON row to `.ai/ledgers/metrics.jsonl` with exactly six fields:

```
{
  "tool": "codex",
  "model": "<resolved Codex model>",
  "exit_code": "<helper result exit_code>",
  "agent": "<node.agent catalog name>",
  "node_id": "<node.id>",
  "duration_s": "<helper result duration_s>"
}
```

This Codex dispatch row intentionally differs from the existing Claude-side row, which uses `tool=<agent_name>`.

## Phase 3 - Output handling

Apply `output.mode`:

| Mode | Behavior | Cost |
| --- | --- | --- |
| `passthrough` | Return the output of `output.node`. Failed and skipped nodes are listed as notes appended to the returned text. | 0 extra LLM calls |
| `synthesize` | Dispatch the `synthesizer` skill via the tool/model in `.ai/models.yaml` `run_pipeline.synthesize` (per the dispatch contract referenced above). Pass the original task, every node's output, the DAG with final statuses, and the failed/skipped sets. | 1 extra LLM call |
| `per-agent` | Return the structured map `{<id>: {status, output}, ...}` without fusion. Skipped and failed nodes carry their status verbatim. | 0 extra LLM calls |

## Wrap-up

1. Persist the filled run to `.ai/agent-runs/<YYYY-MM-DD>-<task_slug>.md`. The packet includes a top-level `pipeline: <name>` field. New file only — never overwrite; if the path exists, append `-2`, `-3`, ... before `.md` until unique. `<YYYY-MM-DD>` is today's UTC date.
2. Append compact JSON metrics rows to `.ai/ledgers/metrics.jsonl`:
   - One `pipeline_dispatch` row per dispatched node (`tool=<agent_name>`, `model=<agent.model from frontmatter>`). Skipped nodes do not emit a row.
   - One `pipeline_synthesis` row only when `output.mode = synthesize` (`tool=claude`, `model=<configured synth model>`).
   - Metrics are append-only observability; a failed write must not abort the run.
3. If the `synthesize` phase produced memory updates, append them as `- YYYY-MM-DD [pipeline] <fact>` lines to `.ai/memory.md`. If the memory size after the append would cross `memory_tuning.consolidation_threshold_lines` from `.ai/project.yaml`, dispatch the `maintenance` skill per `.ai/models.yaml` `run_pipeline.maintenance` to compact memory; otherwise append inline. If there are no updates, report `none`.

Report to the user: final output (per mode), per-node statuses, failed and skipped nodes with reasons, files changed (if any agent changed files), memory updates, and the phase execution log.

## Error table

| Condition | Action |
| --- | --- |
| `.ai/pipelines/<name>.yaml` missing | STOP `pipeline '<name>' not found at .ai/pipelines/<name>.yaml`. |
| Slug fails regex | STOP `invalid slug '<name>' (must match ^[a-z0-9-]+$)`. |
| YAML parse error | STOP `pipeline invalid: <yaml parse error>`. |
| Schema validation failure | STOP and surface every `pipeline invalid: ...` error from the validator. |
| `output.mode = synthesize` but `run_pipeline.synthesize` missing in `.ai/models.yaml` | STOP `run_pipeline.synthesize not configured`. |
| `synthesizer` skill body missing when needed | STOP and report the expected discovery path. |
| Node `agent` unresolvable in catalog | STOP `agent '<x>' not found in any scope at runtime`. |
| Agent Task call failure / empty output | Mark node `failed`; mark dependents `skipped`; continue independent branches; surface in output. |
| All remaining nodes are `skipped` | Halt Phase 2; still run Phase 3 over whatever completed and persist the run. |
| `codex` binary not on PATH when running Codex Phase 2B | STOP with an explicit message naming the missing `codex` binary. |
| `.ai/models.yaml` missing `run_pipeline.codex_dispatch` block when running Codex Phase 2B | STOP `run_pipeline.codex_dispatch not configured`. |
| Helper subprocess crashes or exits non-zero during Codex Phase 2B | STOP and report stderr from `.ai/dashboard/scripts/pipeline_fanout.py`. |

## Notes

- Pipelines are structure-only. Do NOT add per-node prompt templates or variables — prompts are built at runtime from the entry-point task + catalog descriptions + ancestor outputs.
- Plugin agents are read-only catalog entries; dispatching them is fine, editing their files is not.
- The agent catalog is an input to dispatch, not a request to create, remove, or modify agents.
