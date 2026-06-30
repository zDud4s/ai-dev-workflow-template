---
name: orchestrate
description: Run the full workflow pipeline from a single prompt - plan, execute with the configured executor, review if needed, and wrap up. Use this as the primary entry point for any development task.
tools: Read, Glob, Grep, Bash, Task, Write
---

You are the orchestrator. You run the full workflow pipeline end-to-end from one task description.

**Read `.ai/workflow/dispatch.md` once before starting.** It defines the dispatch contract, routing logic (`inline | agent | dispatcher`), prompt-passing convention (temp file -> stdin), resume rule, and dispatch-time error table. Everything below assumes those rules. Do not duplicate them.

**Packets are read-only templates.** `.ai/packets/*.md` is the schema layer: phases READ them and EMIT filled copies in their output. Filled execution packets flow through temp files to the executor and are deleted after dispatch. For medium/large tasks the planner MAY persist a filled plan at `.ai/plans/<YYYY-MM-DD>-<slug>.md` (new file only). You must never call Edit/Write on `.ai/packets/`.

## Discovery path convention

Throughout this skill, "discovery path" means: `.claude/skills/<name>/SKILL.md` if you run as Claude, `~/.agents/skills/<name>/SKILL.md` if you run as Codex.

## Entry point

The user invokes you with `Use the orchestrate skill. Task: [description]`.

## Output format

Returns a Markdown report containing:
- Task summary and outcome
- Files changed (paths and counts)
- Validation results (exit codes and test evidence)
- Unresolved risks (if any)
- Memory updates applied
- Phase execution log (plan, execute, review, wrap-up with tool/model/source columns)

## Pre-flight checks

Stop immediately if any fail: `.ai/models.yaml` exists; `.ai/project.yaml` `project_name` is not `unknown` (otherwise run bootstrap first); `.ai/workflow/dispatch.md` exists; the executor skill for `execute.tool` exists in your discovery path. If missing, use the dispatch error table wording.

Then resolve the **session identity** per dispatch.md's "Session identity" rule: use the model actually running this conversation (normalized — strip any `[...]` variant suffix), not the static `session.model`. If they differ, emit the drift warning and proceed with the running model for all routing and inline attribution.

## Phase 1 - Triage + Plan

Read `plan.tool` and `plan.model` from `.ai/models.yaml`. Build a planner prompt combining the `planner` skill (discovery path), the user task, relevant facts from `project.yaml` / `memory.md` / `decisions.md`, and `.ai/packets/execute.md`. Dispatch through the configured tool/model.

The planner output must state both `Size` and `Risk level` at the top. Size values: `trivial` (single file, <10 lines, low risk only, no packet), `small` (1-3 files), `medium` (4-10 files or crosses subsystems), `large` (>10 files or unclear architecture). Elevated trivial work promotes to `small`.

Risk level controls review. Trust the planner's `Risk level` + `Risk matches`; run Phase 3 if `Risk level: elevated` OR Size is `medium` OR Size is `large`.

If planner output is missing `Size`, missing `Risk level`, or emits `TRIVIAL:` with `Risk level: elevated`, STOP and report invalid planner output.

### Deterministic gate (code changes)

medium/large code changes that are TDD-able default to the deterministic gate. When the planner emits Size `medium`/`large`, `Change type: code`, and `TDD-able: yes`, tell the user that `orchestrate-tdd` is the default route and run under its deterministic-gate/test-first discipline by default.

the gate command's exit code, re-run independently by the orchestrator, is the recorded ship/no-ship decision. Independently re-run the packet's `Validation.Commands` / test command; exit 0 records ship, non-zero records do-not-ship and resumes/rescues, never ships.

For `TDD-able: no` code tasks, stay in normal orchestrate flow with no formal TDD cycle, but still independently run the packet's `Validation.Commands` as the deterministic gate and record that exit code as the decision.

### Auto-select handoff (when `auto_select.enabled: true`)

If `auto_select.enabled: true`, after receiving the planner output: locate the `## Selected models` block. Missing or malformed → STOP per the auto-select rows in dispatch.md's error table. Parse each line into `(phase, tool, model, reasoning_effort?, reason)`; header followed by zero lines = "no match", fall back to `models.yaml` for every downstream phase. Verify each parsed tool is locally available; if not, STOP `auto-selected tool unavailable: <tool> for phase <phase>` — do NOT silently fall back. Record `auto_overrides = { phase: (tool, model, effort, reason) }` for Phases 2-4. If `auto_select.enabled` is `false` or absent, set `auto_overrides = {}`.

## Phase 2 - Execute

If `auto_overrides["execute"]` is set, use its `(tool, model, reasoning_effort)`. Otherwise read `execute.tool`, `execute.model`, and `execute.reasoning_effort` from `.ai/models.yaml`. Dispatch the execution packet (or trivial instruction) through the resolved tool. Tool-specific invocation details live in the skill named after `<execute.tool>` (discovery path); read that skill alongside this one when dispatching. When dispatch routing resolves to `agent`, the controller delegates via the Claude Code Task tool (in-process subagent) instead of a subprocess; see dispatch.md.

### Hard rule: no in-context execution

The orchestrator does not make code changes itself unless `execute` resolves to `inline` per dispatch routing. Never Edit/Write a patch produced by a dispatched executor, never extract a diff from its output and apply it, never offer "let me apply it myself" — if you catch yourself doing any of these, STOP.

### Hard rule: synchronous dispatch only

Every dispatched phase MUST be launched synchronously — the orchestrator blocks until the subprocess exits or its timeout fires. Never use background-launch flags (`run_in_background: true`, shell `&`, `nohup`, `Start-Process` without `-Wait`, etc.). Background mode returns immediately with metadata instead of the phase output, which breaks the Handoff check, dispatch tracker, and metrics row. If a phase needs more than the default timeout, raise `<phase>.timeout_seconds` in `.ai/models.yaml`. See dispatch.md "Synchronous-call invariant".

If the executor fails (timeout, non-zero exit, environment/permission block), only allowed responses: report the exact error; suggest `.ai/models.yaml` or executor-environment fixes; dispatch rescue (`auto_overrides["rescue"]` if set, else `rescue.tool`/`rescue.model` from `.ai/models.yaml`); STOP and wait for user direction.

### Handoff check

Wait for the executor to complete within the configured timeout (see dispatch.md), then handle the exit: timeout -> freeze, rescue, report, stop; non-zero with `## Escalation` -> surface `reason`, `needed`, `suggested-next`, `partial-output`, ask rescue/re-plan/abandon, do not advance; non-zero without escalation -> tooling failure; exit 0 -> inspect `## Handoff`.

Proceed only if `Files changed`, `Tests added`, and `Validation evidence` are complete. `Validation evidence` needs one block per validation command with `$ <cmd>`, `exit:`, and `tail:` lines; concrete `could not run:` reasons are acceptable.

If incomplete, attempt ONE recovery resume using the `<execute.tool>` skill (discovery path). Prompt: "The Handoff section is incomplete. Re-fill it. `Validation evidence` must contain one block per command in Validation.Commands with `$ <cmd>`, `exit: <code>`, `tail: <last 5 lines>`. If a command could not run, state a concrete reason. Output the completed Handoff."

After the resume: incomplete Handoff or non-zero validation -> dispatch rescue, report, stop. Do not advance to review.

## Phase 3 - Review (conditional)

Run if the review gate from Phase 1 says so; otherwise skip.

If `auto_overrides["review"]` is set, use its `(tool, model)`. Otherwise read `review.tool` and `review.model` from `.ai/models.yaml`. Build a reviewer prompt combining the `reviewer` skill (discovery path), the execution packet objective, the executor's filled Handoff, and `.ai/packets/review.md`. Dispatch through the resolved tool/model.

Review authority: where a deterministic gate exists and passed, the reviewer verdict is advisory. Record reviewer `request-changes` / `escalate` findings as advisory annotations under `Risks` and continue; the reviewer's hard-gate checks remain authoritative for validation evidence present, gate ran, exit 0, and tests accounted for because they confirm the gate itself. tasks without a deterministic gate keep the blocking reviewer verdict. This overrides the Verdict handling below for `request-changes` / `escalate` when the gate passed.

`approve` always proceeds to Phase 4 (gate passed or not). Verdict handling for `request-changes` / `escalate` when no deterministic gate passed (gate-less task, or the gate failed): `request-changes` -> show findings and ask send back / accept / stop; `escalate` -> STOP and report full findings + Handoff.

For send-back, re-dispatch the executor with `Reviewer findings:\n<findings>\n\nOriginal objective: <objective>` via temp file -> stdin — a fresh write-capable `execute` dispatch, NOT `resume --last` (resume can't carry the bypass flag, so it stalls codex's sandbox and fails the permission allow rule; see dispatch.md's resume rule). The executor's prior edits are already in the tree, so nothing is lost. Re-run review and increment N. There is no automatic cap; warn from iteration 5 onward.

If accepted without changes, surface unresolved findings under `Risks` with "Reviewer findings accepted without changes (iteration N)".

## Phase 4 - Wrap up

1. **Pending deletions.** Ask for confirmation before any deletion; report declined deletions as unresolved.
2. **Memory updates.** Collect executor/reviewer updates and append them to `.ai/memory.md`. Maintenance auto-detects whether a consolidation pass is needed (size, contradictions, density triggers in its SKILL — no flag from the orchestrator). Dispatch maintenance only when there are pending updates to apply.
3. **Report to user:** Summary, Files changed, Validation, Risks, Memory updates applied, and Phase execution log. Per-phase log line columns: `tool`, `model`, `source=auto|config` (`auto` when the value came from the planner's `## Selected models` block, `config` when from `.ai/models.yaml`), and when `source=auto`, the `reason` from the planner. `configured`, `resolved`, and `command` columns are unchanged.

Examples:
`plan tool=claude model=claude-opus-4-7 source=config configured=auto resolved=inline command=inline`
`execute tool=codex model=gpt-5.5 source=auto reason="small/low/medium-budget" configured=auto resolved=dispatcher command=codex exec ...`

## Dispatched-phase prompt contents

When you build a delegated prompt for any phase, include ONLY:
- the phase skill body (from discovery path)
- the relevant packet schema from `.ai/packets/`
- `project.yaml`
- the user task / current objective
- the relevant slice of `memory.md` (see "Memory slice" below)

Do NOT include: `dispatch.md`, this skill, or any other phase skill. The dispatch contract is yours alone; dispatched phases only need their own skill plus the schema they will fill.

### Memory slice

After Phase 1, parse the planner's `Memory tags: [tag1, tag2, ...]` line. For Phases 2-4 dispatched prompts, inject only `memory.md` entries whose topic tag matches the list — e.g. `grep -E '^\- [0-9-]+ \[(tag1|tag2|tag3)\]' .ai/memory.md`. Empty list (`Memory tags: []`) or missing line = inject full `memory.md` (fallback). Always include the file header + format docstring so the dispatched phase still understands the entry format.

## Metrics logging

After every dispatched phase completes (regardless of `auto_select.enabled`), append one JSON line to `.ai/local/ledgers/metrics.jsonl`. Gitignored, append-only, observability — never abort the pipeline if writing the line fails. Source data for the adaptive scorer (PR 3).

Schema (one JSON object per line, compact, no pretty-print):

```
{"ts":"<ISO 8601 UTC, Z suffix>","task_slug":"<slug>","phase":"<plan|execute|review|rescue|maintenance>","tool":"<tool>","model":"<model>","reasoning_effort":"<low|medium|high|xhigh|null>","size":"<trivial|small|medium|large|null>","risk":"<low|elevated|null>","budget":"<low|medium|high|null>","exit_code":<int>,"duration_ms":<int>,"handoff_complete":<bool|null>,"review_verdict":"<approve|request-changes|escalate|null>","retries":<int>,"tokens_in":<int|null>,"tokens_out":<int|null>,"cache_read":<int|null>,"cache_creation":<int|null>}
```

Field rules: `ts` at subprocess return (or inline completion); `task_slug` lowercased+hyphenated, same across phases; `tool`/`model`/`reasoning_effort` post-`auto_overrides`; `size`/`risk`/`budget` `null` for `plan`; for non-`plan` rows, `budget` records the `effective_budget` the selector decided on: configured `auto_select.token_budget` after the one-rung upgrade (`low`→`medium`→`high`, `high` stays) when Risk is elevated or Size is large, not the raw configured budget; `exit_code` `0` on inline success; `duration_ms` wall-clock from dispatch start; `handoff_complete` only for `execute` (else `null`); `review_verdict` only for `review` (else `null`); `retries` = recovery resumes + review send-backs.

Token capture rules: dispatcher + codex populates `tokens_out` from the printed "tokens used" total; `tokens_in`/`cache_read`/`cache_creation` stay null because codex prints one total only. Dispatcher + claude populates all four from `--output-format json` `.usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`). Agent (Task tool) + inline leave all token fields null: the Task tool does not return a token count to the controller, and inline shares the session with no isolable per-phase usage.

Append exactly one line per dispatched phase, never overwrite or reorder. Create the file on first append.

## Pipeline error table

Dispatch-layer errors (missing config, tool unavailable, unrecognized values, session-block issues) are in `.ai/workflow/dispatch.md`. The rows below are pipeline-specific.

Planner output missing `Size` or `Risk level`, or elevated `TRIVIAL:` -> STOP and report invalid planner output. Executor timeout -> see dispatch.md timeout row; freeze, rescue, no auto-retry. Executor non-zero with escalation -> surface four fields. Executor non-zero without escalation -> Phase 2 allowed options only. Handoff incomplete or non-zero validation -> rescue and stop. Reviewer `request-changes`, when no deterministic gate passed -> ask send back / accept / stop; when a gate passed, annotate per the Phase 3 advisory rule; warn from iteration 5. Reviewer `escalate`, when no deterministic gate passed -> stop and report full context; when a gate passed, annotate per the Phase 3 advisory rule.
