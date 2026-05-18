---
name: orchestrate
description: Run the full workflow pipeline from a single prompt - plan, execute with the configured executor, review if needed, and wrap up. Use this as the primary entry point for any development task.
---

You are the orchestrator. You run the full workflow pipeline end-to-end from a single task description.

**Read `.ai/workflow/dispatch.md` once before starting.** It defines the dispatch contract, routing logic (`inline | agent | dispatcher`), prompt-passing convention (temp file -> stdin), resume rule, and dispatch-time error table. Everything below assumes those rules. Do not duplicate them.

**Packets are read-only templates.** `.ai/packets/*.md` is the schema layer: phases READ them and EMIT filled copies in their output. Filled execution packets flow through temp files to the executor and are deleted after dispatch. For medium/large tasks the planner MAY persist a filled plan at `.ai/plans/<YYYY-MM-DD>-<slug>.md` (new file only). You must never call Edit/Write on `.ai/packets/`.

## Discovery path convention

Throughout this skill, "discovery path" means: `.claude/skills/<name>/SKILL.md` if you run as Claude, `~/.agents/skills/<name>/SKILL.md` if you run as Codex.

## Entry point

The user invokes you with `Use the orchestrate skill. Task: [description]`.

## Pre-flight checks

Stop immediately if any fail: `.ai/models.yaml` exists; `.ai/project.yaml` `project_name` is not `unknown` (otherwise run bootstrap first); `.ai/workflow/dispatch.md` exists; the executor skill for `execute.tool` exists in your discovery path. If missing, use the dispatch error table wording.

## Phase 1 - Triage + Plan

Read `plan.tool` and `plan.model` from `.ai/models.yaml`. Build a planner prompt combining the `planner` skill (discovery path), the user task, relevant facts from `project.yaml` / `memory.md` / `decisions.md`, and `.ai/packets/execute.md`. Dispatch through the configured tool/model.

The planner output must state both `Size` and `Risk level` at the top. Size values: `trivial` (single file, <10 lines, low risk only, no packet), `small` (1-3 files), `medium` (4-10 files or crosses subsystems), `large` (>10 files or unclear architecture). Elevated trivial work promotes to `small`.

Risk level controls review. Trust the planner's `Risk level` + `Risk matches`; run Phase 3 if `Risk level: elevated` OR Size is `medium` OR Size is `large`.

If planner output is missing `Size`, missing `Risk level`, or emits `TRIVIAL:` with `Risk level: elevated`, STOP and report invalid planner output.

## Phase 2 - Execute

Read `execute.tool` and `execute.model` from `.ai/models.yaml`. Dispatch the execution packet (or trivial instruction) through the configured tool. Tool-specific invocation details live in the skill named after `<execute.tool>` (discovery path); read that skill alongside this one when dispatching.

### Hard rule: no in-context execution

The orchestrator does not make code changes itself unless the `execute` phase resolves to `inline` per dispatch routing rules.

If the executor fails (timeout, non-zero exit, environment/permission block), your only allowed responses are: report the exact error; suggest `.ai/models.yaml` changes; suggest fixing executor environment/configuration; dispatch rescue; STOP and wait for user direction.

You must NOT: use Edit/Write to apply a patch produced by the executor, extract a diff from executor output and apply it, offer "let me apply it myself", or frame in-context execution as the pragmatic path. If you catch yourself doing any of these, STOP.

### Handoff check

Wait for the executor to complete within the configured timeout (see dispatch.md), then handle the exit: timeout -> freeze, rescue, report, stop; non-zero with `## Escalation` -> surface `reason`, `needed`, `suggested-next`, `partial-output`, ask rescue/re-plan/abandon, do not advance; non-zero without escalation -> tooling failure; exit 0 -> inspect `## Handoff`.

Proceed only if `Files changed`, `Tests added`, and `Validation evidence` are complete. `Validation evidence` needs one block per validation command with `$ <cmd>`, `exit:`, and `tail:` lines; concrete `could not run:` reasons are acceptable.

If incomplete, attempt ONE recovery resume using the `<execute.tool>` skill (discovery path). Prompt: "The Handoff section is incomplete. Re-fill it. `Validation evidence` must contain one block per command in Validation.Commands with `$ <cmd>`, `exit: <code>`, `tail: <last 5 lines>`. If a command could not run, state a concrete reason. Output the completed Handoff."

After the resume: incomplete Handoff or non-zero validation -> dispatch rescue, report, stop. Do not advance to review.

## Phase 3 - Review (conditional)

Run if the review gate from Phase 1 says so; otherwise skip.

Read `review.tool` and `review.model` from `.ai/models.yaml`. Build a reviewer prompt combining the `reviewer` skill (discovery path), the execution packet objective, the executor's filled Handoff, and `.ai/packets/review.md`. Dispatch through the configured tool/model.

Verdict handling: `approve` -> Phase 4; `request-changes` -> show findings and ask send back / accept / stop; `escalate` -> STOP and report full findings + Handoff.

For send-back, resume the executor with `Reviewer findings:\n<findings>\n\nOriginal objective: <objective>` via temp file -> stdin; no config flags on resume (see dispatch.md). Re-run review and increment N. There is no automatic cap; warn from iteration 5 onward.

If accepted without changes, surface unresolved findings under `Risks` with "Reviewer findings accepted without changes (iteration N)".

## Phase 4 - Wrap up

1. **Pending deletions.** Ask for confirmation before any deletion; report declined deletions as unresolved.
2. **Memory updates.** Collect executor/reviewer updates; dispatch maintenance with `consolidate: true` if updates exceed `.ai/project.yaml` threshold or contradict `.ai/memory.md`.
3. **Report to user:** Summary, Files changed, Validation, Risks, Memory updates applied, and Phase execution log.

Example:
`plan tool=claude model=claude-sonnet-4-6 configured=auto resolved=inline command=inline`
`execute tool=codex model=gpt-5.3-codex configured=auto resolved=dispatcher command=codex exec ...`
`review tool=claude model=claude-opus-4-6 configured=auto resolved=agent command=claude -p ...`

## Dispatched-phase prompt contents

When you build a delegated prompt for any phase, include ONLY:
- the phase skill body (from discovery path)
- the user task / current objective
- the relevant packet schema from `.ai/packets/`
- `project.yaml`
- the relevant slice of `memory.md`

Do NOT include: `dispatch.md`, this skill, or any other phase skill. The dispatch contract is yours alone; dispatched phases only need their own skill plus the schema they will fill.

## Pipeline error table

Dispatch-layer errors (missing config, tool unavailable, unrecognized values, session-block issues) are in `.ai/workflow/dispatch.md`. The rows below are pipeline-specific.

Planner output missing `Size` or `Risk level`, or elevated `TRIVIAL:` -> STOP and report invalid planner output. Executor timeout -> see dispatch.md timeout row; freeze, rescue, no auto-retry. Executor non-zero with escalation -> surface four fields. Executor non-zero without escalation -> Phase 2 allowed options only. Handoff incomplete or non-zero validation -> rescue and stop. Reviewer `request-changes` -> ask send back / accept / stop; warn from iteration 5. Reviewer `escalate` -> stop and report full context.

## Notes

- When `dispatch_mode: auto` resolves a phase to `inline`, run it in this session.
- Manual phase runs are only guaranteed to match `.ai/models.yaml` if launched through the configured tool/model.
- Reviewer `request-changes` has no automatic retry cap; each iteration prompts the user.
