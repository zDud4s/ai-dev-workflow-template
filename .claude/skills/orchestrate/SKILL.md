---
name: orchestrate
description: Run the full workflow pipeline from a single prompt - plan, execute with the configured executor, review if needed, and wrap up. Use this as the primary entry point for any development task.
---

You are the orchestrator. You run the full workflow pipeline end-to-end from a single task description.

**Read `.ai/workflow/dispatch.md` once before starting.** It defines the dispatch contract, routing logic (`inline | agent | dispatcher`), the prompt-passing convention (temp file → stdin), the resume rule (no config flags on `resume --last`), and the dispatch-time error table. Everything below assumes those rules. Do not duplicate them.

**Packets are read-only templates.** `.ai/packets/*.md` is the schema layer — phases READ them and EMIT filled copies in their output. Filled execution packets flow through temp files (e.g. `/tmp/execute-packet.md`) to the executor and are deleted after dispatch. For medium/large tasks the planner MAY persist a filled plan at `.ai/plans/<YYYY-MM-DD>-<slug>.md` (new file only). You must never call Edit/Write on any file under `.ai/packets/`. See the Layer model in `.ai/workflow/claude-workflow.md`.

## Entry point

The user invokes you with:
```
Use the orchestrate skill.

Task: [description]
```

## Pre-flight checks

Stop immediately if any of these fail:

1. `.ai/models.yaml` exists.
2. `.ai/project.yaml` `project_name` is not `unknown` (otherwise run bootstrap first).
3. `~/.agents/skills/call-claude/SKILL.md` exists.
4. `.ai/workflow/dispatch.md` exists (this skill depends on it). If missing → STOP: "`.ai/workflow/dispatch.md` not found. Run `install.sh` (or `update-workflow.sh`) to install the dispatch contract."

Specific messages for each are in the dispatch error table.

## Phase 1 — Triage + Plan

Read `plan.tool` and `plan.model` from `.ai/models.yaml`. Build a planner prompt combining `.claude/skills/planner/SKILL.md`, the user task, relevant facts from `project.yaml` / `memory.md` / `decisions.md`, and the `.ai/packets/execute.md` schema. Dispatch through the configured tool/model.

The planner output must state both `Size` and `Risk level` at the top.

**Size** controls plan complexity:

- **trivial** — single file, <10 lines, no cross-cutting concern. Planner emits a one-line instruction (no packet). Allowed ONLY when Risk level is `low`; if Risk level is `elevated`, the planner must promote to `small`.
- **small** — 1-3 files, clear scope. Minimal execution packet.
- **medium** — 4-10 files or crosses subsystems. Full execution packet.
- **large** — >10 files or unclear architecture. Full execution packet.

**Risk level** controls the review gate. The planner computes it by intersecting `Relevant files` with `boundaries.risky_areas`, `security_sensitive`, `migration_sensitive`. The orchestrator does NOT re-check — trust the planner's `Risk level` + `Risk matches`.

**Review gate (single rule):** run Phase 3 if `Risk level: elevated` OR Size is `medium` OR Size is `large`. Skip otherwise.

If the planner output is missing `Size`, missing `Risk level`, or emits `TRIVIAL:` with `Risk level: elevated`, STOP and report invalid planner output.

## Phase 2 — Execute

Read `execute.tool` and `execute.model` from `.ai/models.yaml`. Dispatch the execution packet (or trivial instruction) through the configured tool. Tool-specific invocation details (flags, sandbox bypass, resume mechanics, output filtering) live in `.claude/skills/<execute.tool>/SKILL.md` — read that skill alongside this one when dispatching. For example, the codex skill specifies `--dangerously-bypass-approvals-and-sandbox` for write tasks; other executors will have their own conventions.

### Hard rule: no in-context execution

The orchestrator does not make code changes itself unless the `execute` phase resolves to `inline` per the dispatch routing rules. When `execute` does not resolve to inline, all code changes must come from the configured tool.

If the executor fails (timeout, non-zero exit, environment/permission block from the tool), your only allowed responses are:

1. Report the exact error to the user.
2. Suggest the user update `.ai/models.yaml` to use an available tool.
3. Suggest the user fix the executor's environment or configuration (see the tool's skill for diagnostics).
4. Dispatch the rescue phase using `rescue.tool` and `rescue.model`.
5. STOP and wait for user direction.

You must NOT: use Edit/Write to apply a patch produced by the executor, extract a diff from the executor's output and apply it, offer "let me apply it myself" as an option, or frame in-context execution as the pragmatic path. If you catch yourself doing any of these, STOP.

### Handoff check

Wait for the executor to complete (within the configured timeout — see dispatch.md). Then handle the exit:

- **Timed out (exit 124 / wrapper kill)** → freeze. Dispatch rescue with the timeout context, report to user, stop. Do not auto-retry.
- **Non-zero exit with `## Escalation` block in output** → structured escalation. Parse the four fields (`reason`, `needed`, `suggested-next`, `partial-output`), surface them to the user verbatim, and ask whether to dispatch rescue, re-plan, or abandon. Do not advance to review.
- **Non-zero exit without escalation block** → crash / environment / tooling failure. Follow the Phase 2 allowed responses above.
- **Exit 0** → proceed to the Handoff check below.

Look for the filled `## Handoff` section (fields: Files changed, Tests added, Validation evidence, Deviations from plan, New risks discovered, Memory updates, Pending deletions).

Proceed only if ALL:
- `Files changed` is filled, AND
- `Tests added` is filled — and if the planning packet's `Tests to add` was non-empty, every planned test must be marked `added` or carry a concrete skip reason (vague skips like "did not test" fail this check), AND
- `Validation evidence` contains one block per command in `Validation.Commands`, each with `$ <cmd>`, `exit:`, and `tail:` lines. A command may instead carry `could not run: <reason>` — accept only if the reason is concrete (e.g. "tool not installed in execution environment"), reject vague ones ("did not test").

If incomplete, attempt ONE recovery resume. The resume mechanism is tool-specific — consult `.claude/skills/<execute.tool>/SKILL.md`. The prompt to send is:

> "The Handoff section is incomplete. Re-fill it. `Validation evidence` must contain one block per command in Validation.Commands with `$ <cmd>`, `exit: <code>`, `tail: <last 10 lines>`. If a command could not run, state a concrete reason. Output the completed Handoff."

Example for codex (other executors invoke resume differently — see their skill):

```bash
# Codex-specific. Defer to .claude/skills/codex/SKILL.md for current conventions.
printf '%s' '<resume prompt above>' | codex exec --skip-git-repo-check resume --last 2>/dev/null
```

After the resume:
- Handoff still incomplete → dispatch rescue, report, stop.
- `Validation evidence` shows non-zero exit on a command that actually ran → executor failure: dispatch rescue, report, stop. Do not advance to review.

## Phase 3 — Review (conditional)

Run if the review gate from Phase 1 says so. Skip otherwise.

Read `review.tool` and `review.model` from `.ai/models.yaml`. Build a reviewer prompt combining `.claude/skills/reviewer/SKILL.md`, the execution packet objective, the executor's filled Handoff, and `.ai/packets/review.md`. Dispatch through the configured tool/model.

Verdict handling:

- **approve** → proceed to Phase 4.

- **request-changes** → show findings to the user and ask:
  > "The reviewer found issues (iteration N). Options: (1) send back to the executor for another pass, (2) accept current state and proceed to wrap-up, (3) stop."

  - **(1) send back** → resume the executor session with `Reviewer findings:\n<findings>\n\nOriginal objective: <objective>` via temp file → stdin (no config flags on resume — see dispatch.md). Resume mechanics are tool-specific (`.claude/skills/<execute.tool>/SKILL.md`). Re-run the reviewer on the new Handoff. The new verdict goes through this same verdict handler — increment iteration N when re-asking. There is no automatic cap; the user gates each additional pass.
  - **(2) accept** → proceed to Phase 4. In the wrap-up report, surface the unresolved reviewer findings under `Risks` with the line "Reviewer findings accepted without changes (iteration N)".
  - **(3) stop** → STOP, report findings as-is.

  Track iteration N across the loop and include it in every prompt so the user sees cost accumulating. If N reaches 5, prepend a warning to the prompt: "This is iteration 5 — consider whether the plan itself is wrong before continuing."

- **escalate** → STOP, report full findings and Handoff context.

## Phase 4 — Wrap up

1. **Pending deletions.** Check the Handoff `Pending deletions` field.
   - Non-empty → ask: *"The executor flagged these for deletion. Confirm: [list]"*
   - Confirmed → execute each deletion.
   - Declined → report as unresolved and skip.

2. **Memory updates.** Collect from executor Handoff (`Memory updates`) and reviewer output (`Memory updates to apply`, if review ran).
   - Read the current threshold from `.ai/project.yaml` at `memory_tuning.consolidation_threshold_lines` (default 150 if the block is absent).
   - Check `.ai/memory.md` line count. If `current_lines + new_updates > threshold`, include `consolidate: true` in the maintenance prompt so the skill runs a consolidation pass before appending.
   - If any memory updates contradict an existing entry (same `[topic]`, incompatible fact), include `consolidate: true` regardless of size.
   - Dispatch the maintenance phase using `maintenance.tool` and `maintenance.model`. If consolidation ran, surface its summary in the final report under "Memory updates applied", including the `Threshold update` line (old → new and the smoothed ratio).

3. **Report to user:**
   - **Summary** — what was done (1-3 sentences)
   - **Files changed** — list from Handoff
   - **Validation** — commands run and result
   - **Risks** — anything noted by executor or reviewer
   - **Memory updates applied** — list or "none"
   - **Phase execution log** — for each phase that ran:
     - `tool` / `model` from config
     - `configured`: per-phase `mode` if set, otherwise the top-level `dispatch_mode`
     - `resolved`: final mode (`inline` / `agent` / `dispatcher`)
     - actual command (or "inline" if no subprocess)

     Example:
     ```
     plan      tool=claude  model=claude-sonnet-4-6  configured=auto  resolved=inline      command=inline
     execute   tool=codex   model=gpt-5.3-codex       configured=auto  resolved=dispatcher  command=codex exec ...
     review    tool=claude  model=claude-opus-4-6     configured=auto  resolved=agent       command=claude -p ...
     ```

## Pipeline error table

Dispatch-layer errors (missing config, tool unavailable, unrecognized values, session-block issues) are in `.ai/workflow/dispatch.md`. The rows below are pipeline-specific.

| Situation | Action |
|---|---|
| Planner output missing `Size` | STOP — report invalid planner output |
| Planner output missing `Risk level` | STOP — report invalid planner output |
| Planner emits `TRIVIAL:` but `Risk level: elevated` | STOP — planner should have promoted to `small` |
| Executor timed out | See dispatch.md timeout row — freeze, dispatch rescue, no auto-retry. |
| Executor non-zero with `## Escalation` block | Surface the four escalation fields to the user; ask whether to rescue, re-plan, or abandon. Do not advance to review. |
| Executor non-zero without escalation block | Crash / environment / permission failure. Follow Phase 2 allowed options. Never extract and apply patches. |
| Handoff absent or `Validation evidence` incomplete after one resume | Dispatch rescue, report, stop |
| `Validation evidence` shows non-zero exit on a command that ran | Dispatch rescue, report, stop. Do not advance to review. |
| Reviewer `request-changes` (any iteration) | Ask user: send back / accept / stop. No automatic cap. Warn from iteration 5 onward. |
| Reviewer `escalate` | Stop, report full context |

## Notes

- When `dispatch_mode: auto` resolves a phase to `inline`, run it in this session — that is the token-efficient path when tool and model match.
- Manual phase runs from outside the orchestrator are only guaranteed to match `.ai/models.yaml` if launched through the configured tool/model.
- Reviewer `request-changes` has no automatic retry cap. Each iteration prompts the user with three options (send back / accept / stop). The iteration counter is shown in every prompt; from iteration 5 onward a warning is prepended to make the user re-evaluate the plan itself.
