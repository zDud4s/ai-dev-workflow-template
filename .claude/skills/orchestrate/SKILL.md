---
name: orchestrate
description: Run the full workflow pipeline from a single prompt - plan, execute with Codex, review if needed, and wrap up. Use this as the primary entry point for any development task.
---

You are the orchestrator.

Your job is to run the full workflow pipeline end-to-end from a single task description.

## Entry point

The user invokes you with:
```
Use the orchestrate skill.

Task: [description]
```

## Pre-flight checks

Before starting any phase, verify all of the following. Stop immediately if any check fails.

1. `.ai/models.yaml` exists. If not -> STOP: "`.ai/models.yaml` not found. Run `install.sh` first."
2. `.ai/project.yaml` `project_name` is not `unknown`. If it is -> STOP: "Project not bootstrapped. Run the bootstrap skill first."
3. `~/.agents/skills/call-claude/SKILL.md` exists. If not -> STOP: "Codex-Claude bridge skill not installed. Run `install.sh` first."

## Dispatch contract

The starter session is an orchestrator only. It does triage coordination, prompt assembly, dispatch, and result validation. It does not substitute its own model for any configured workflow phase.

For every workflow phase (`plan`, `execute`, `review`, `rescue`, `maintenance`, `bootstrap`):

1. Read `<phase>.tool` and `<phase>.model` from `.ai/models.yaml`.
2. Build a standalone prompt packet for that phase. Include:
   - the relevant skill instructions from `.claude/skills/<phase>/SKILL.md` when available
   - the current objective
   - the required repo context
   - the relevant packet schema from `.ai/packets/`
3. Execute that phase through the configured tool and model.
4. Capture and retain:
   - phase name
   - requested tool
   - requested model
   - actual command used
   - exit status
5. If the configured tool is unavailable, STOP and tell the user to update `.ai/models.yaml` or fix the tool installation. Do not substitute the current session model.

Never run a configured phase "within this context window" as a shortcut, even if the current session appears to use the same model. The only way to guarantee the phase model is to launch the configured tool with the configured model explicitly.

## Dispatcher rules

The subprocess does not inherit this session automatically. Do not send a bare instruction like "Use the planner skill." Inline the relevant skill instructions and task context into the delegated prompt.

Use temp files and stdin for all delegated prompts to avoid shell quoting issues.

- If `<phase>.tool` is `claude`:
  ```bash
  cat /tmp/phase-prompt.md | claude -p "Execute the attached <phase> phase exactly. Return only the phase result." --model <phase.model> 2>/dev/null
  ```
- If `<phase>.tool` is `codex`:
  ```bash
  cat /tmp/phase-prompt.md | codex exec --skip-git-repo-check \
    -m <phase.model> \
    --config model_reasoning_effort="medium" \
    -C <absolute path to project directory> \
    2>/dev/null
  ```

If either command exits non-zero, treat that phase as failed and follow the error policy for that phase.

## Phase 1 - Triage + Plan

Read `plan.tool` and `plan.model` from `.ai/models.yaml`.

Construct a standalone planner prompt by combining:
- `.claude/skills/planner/SKILL.md`
- the user task
- relevant facts from `.ai/project.yaml`, `.ai/memory.md`, and `.ai/decisions.md`
- the packet schema from `.ai/packets/execute.md`

Dispatch the planner phase through the configured tool/model.

The planner output must classify task size as one of:

- **trivial** - single file, <10 lines, no cross-cutting risk:
  - Produce a single instruction string (no execution packet).
  - Go directly to Phase 2 passing the instruction as the Codex prompt.
  - Skip Phase 3 entirely.
  - Phase 4: minimal wrap-up (report files changed only; skip memory updates unless Codex's Handoff `Memory updates` field is non-empty).

- **small** - 1-3 files, clear scope:
  - Produce a minimal execution packet using `.ai/packets/execute.md` schema.
  - After Phase 2, check if any file in `Allowed files` appears in `.ai/project.yaml` under `boundaries.risky_areas`, `boundaries.security_sensitive`, or `boundaries.migration_sensitive`.
  - If those lists are all empty -> skip Phase 3.
  - If any match -> run Phase 3.

- **medium** - 4-10 files or crosses subsystem boundaries:
  - Produce full execution packet using `.ai/packets/execute.md` schema.
  - Phase 3 is mandatory.

- **large** - >10 files, unclear architecture, or touches risky/security-sensitive areas:
  - Produce full execution packet using `.ai/packets/execute.md` schema.
  - Phase 3 is mandatory.

If the planner output does not clearly state the size at the top, STOP and report invalid planner output.

## Phase 2 - Execute (Codex)

Read `execute.tool` and `execute.model` from `.ai/models.yaml`.

**HARD RULE - No in-context execution. Ever.**
- You are the orchestrator. You must not make code changes yourself.
- Execution must go through `codex exec`.
- If the tool specified in `models.yaml` is not available (for example, Codex CLI not installed, no subscription) -> STOP and tell the user to update `.ai/models.yaml` to use a tool that is available.
- This rule has no exceptions. Violating it invalidates the entire pipeline.

**Forbidden actions - if you catch yourself doing any of these, STOP immediately:**
- Using Edit, Write, or any file-modification tool to apply code changes from a Codex patch or plan
- Offering "Let me execute directly" or "Let me apply the changes myself" as an option to the user
- Saying "Codex produced the correct patch but was blocked by sandbox write policy. Let me apply its patch directly."
- Extracting diff or patch content from Codex output and applying it yourself
- Presenting "execute directly" or "apply it myself" as one of the options when Codex fails
- Framing in-context execution as "the pragmatic path" or any other euphemism

**When Codex fails (sandbox error, non-zero exit, write policy block), your only allowed responses are:**
1. Report the exact error to the user
2. Suggest the user update `.ai/models.yaml` to use an available tool
3. Suggest the user fix the Codex environment or configuration
4. Dispatch the rescue phase using `rescue.tool` and `rescue.model`
5. STOP and wait for user direction

Run Codex with the execution packet (or trivial instruction) as the prompt.

**Passing the prompt safely:** Shell quoting breaks when packet content contains single quotes, double quotes, backticks, or other special characters. Always write the prompt to a temp file and pipe it via stdin:

```bash
# 1. Write packet content to a temp file
#    e.g. /tmp/codex-packet.md

# 2. Pipe the file into codex exec
cat /tmp/codex-packet.md | codex exec --skip-git-repo-check \
  -m <execute.model from models.yaml> \
  --config model_reasoning_effort="medium" \
  --dangerously-bypass-approvals-and-sandbox \
  -C <absolute path to project directory> \
  2>/dev/null

# 3. Clean up the temp file
rm -f /tmp/codex-packet.md
```

Never pass packet contents as a positional argument or inside a heredoc - use the temp-file-to-stdin approach above.

Wait for Codex to complete and capture its full output.

**Handoff check:**
- Look for the filled `## Handoff` section in Codex output (fields: Files changed, Actual commands run, Deviations from plan, New risks discovered, Memory updates).
- If Handoff is present and at least `Files changed` is filled -> proceed.
- If Handoff is absent or all fields are empty -> attempt one recovery resume:
  ```bash
  printf '%s' 'The Handoff section was not filled. Please fill all fields in the Handoff section of the execution packet and output the result.' | codex exec --skip-git-repo-check resume --last 2>/dev/null
  ```
  - If Handoff is still absent after one resume -> dispatch the rescue phase using `rescue.tool` and `rescue.model`, report findings to user, stop.
- If Codex exits non-zero or reports sandbox or write-policy errors -> dispatch the rescue phase using `rescue.tool` and `rescue.model`, report findings to user, stop. Never parse Codex output to extract a patch and apply it yourself.

## Phase 3 - Review (conditional)

Skip if: trivial, or small with no risky, security-sensitive, or migration-sensitive files matched.

Run if: medium, large, or small with risky files matched.

Read `review.tool` and `review.model` from `.ai/models.yaml`.

Construct a standalone reviewer prompt by combining:
- `.claude/skills/reviewer/SKILL.md`
- the execution packet objective
- the executor's filled Handoff
- `.ai/packets/review.md`

Dispatch the reviewer phase through the configured tool/model. Do not review inside the starter session.

**Verdict handling:**

- **approve** -> proceed to Phase 4.

- **request-changes** -> show the reviewer's findings to the user and ask:
  > "The reviewer found issues. Do you want me to send these back to Codex for fixes? (yes/no)"

  - If **yes** -> resume Codex (no config flags on resume). Write the resume prompt to a temp file first to avoid quoting issues, then pipe it:
    ```bash
    # Write resume prompt to temp file
    # Content: "Reviewer findings:\n<findings>\n\nOriginal objective: <objective>"
    cat /tmp/codex-resume.md | codex exec --skip-git-repo-check resume --last 2>/dev/null
    rm -f /tmp/codex-resume.md
    ```
    Re-run the reviewer through `review.tool` and `review.model` on the new Handoff.
    - If verdict is still `request-changes` -> stop. Report full reviewer findings and Handoff to user. Do not loop further.
    - If verdict is `approve` -> proceed to Phase 4.

  - If **no** -> stop. Report reviewer findings to user as-is.

- **escalate** -> stop. Report full reviewer findings and Handoff context to user.

## Phase 4 - Wrap up

1. **Pending deletions:** Check the Handoff `Pending deletions` field.
   - If non-empty -> show the list to the user and ask:
     > "The executor flagged these files/dirs for deletion. Confirm to proceed: [list]"
   - If confirmed -> execute each deletion.
   - If declined -> report them as unresolved and skip.

2. Collect memory updates:
   - From executor Handoff: `Memory updates` field.
   - From reviewer output: `Memory updates to apply` field (if review ran).
3. If any memory updates exist -> dispatch the maintenance phase using `maintenance.tool` and `maintenance.model` to append them to `.ai/memory.md`.
4. Report to user:
   - **Summary:** what was done (1-3 sentences)
   - **Files changed:** list from Handoff
   - **Validation:** commands run and result
   - **Risks:** any risks noted by executor or reviewer
   - **Memory updates applied:** list or "none"
   - **Phase execution log:** requested tool/model and actual command used for each phase that ran

## Error table

| Situation | Action |
|-----------|--------|
| `models.yaml` missing | STOP - tell user to run `install.sh` |
| `project_name` is `unknown` | STOP - tell user to run bootstrap skill |
| `call-claude` skill missing at `~/.agents/skills/call-claude/` | STOP - tell user to run `install.sh` |
| Tool from `models.yaml` unavailable | STOP - tell user to update `.ai/models.yaml` to use an available tool. Never execute in-context as fallback. |
| Planner output missing size classification | STOP - report invalid planner output |
| Codex exits non-zero | Dispatch rescue using `rescue.tool` and `rescue.model`, report error + allowed options, stop. Never extract and apply patches yourself. |
| Codex blocked by sandbox or write policy | Same as non-zero exit. Report the error and the 5 allowed responses from Phase 2. Do not apply the patch in-context. |
| Handoff absent after one resume | Dispatch rescue using `rescue.tool` and `rescue.model`, report, stop |
| Reviewer `request-changes` twice | Stop, report full context to user |
| Reviewer `escalate` | Stop, report full context to user |

## Notes

- `plan`, `execute`, `review`, `rescue`, `maintenance`, and `bootstrap` must all run through the tool and model configured in `.ai/models.yaml`.
- The starter session never substitutes its own model for a configured phase.
- Manual phase runs are only guaranteed to match `.ai/models.yaml` if you launch them through the configured tool/model as well.
- Never pass config flags (`-m`, `--config`, `--sandbox`, `--full-auto`) when using `resume --last`. Only the prompt is passed.
- The retry cap for `request-changes` is exactly 1. Do not loop beyond that.
