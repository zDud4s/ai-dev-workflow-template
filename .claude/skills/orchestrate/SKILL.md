---
name: orchestrate
description: Run the full workflow pipeline from a single prompt — plan, execute with Codex, review if needed, and wrap up. Use this as the primary entry point for any development task.
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

1. `.ai/models.yaml` exists. If not → STOP: "`.ai/models.yaml` not found. Run `install.sh` first."
2. `.ai/project.yaml` `project_name` is not `unknown`. If it is → STOP: "Project not bootstrapped. Run the bootstrap skill first."
3. `~/.agents/skills/call-claude/SKILL.md` exists. If not → STOP: "Codex→Claude bridge skill not installed. Run `install.sh` first."

## Phase 1 — Triage + Plan (internal)

Run the planner skill rules **within this context window** (no subprocess, no `claude -p` call).

Classify task size:

- **trivial** — single file, <10 lines, no cross-cutting risk:
  - Produce a single instruction string (no execution packet).
  - Go directly to Phase 2 passing the instruction as the Codex prompt.
  - Skip Phase 3 entirely.
  - Phase 4: minimal wrap-up (report files changed only; skip memory updates unless Codex's Handoff `Memory updates` field is non-empty).

- **small** — 1-3 files, clear scope:
  - Produce a minimal execution packet using `.ai/packets/execute.md` schema.
  - After Phase 2, check if any file in `Allowed files` appears in `.ai/project.yaml` under `boundaries.risky_areas`, `boundaries.security_sensitive`, or `boundaries.migration_sensitive`.
  - If those lists are all empty → skip Phase 3.
  - If any match → run Phase 3.

- **medium** — 4-10 files or crosses subsystem boundaries:
  - Produce full execution packet using `.ai/packets/execute.md` schema.
  - Phase 3 is mandatory.

- **large** — >10 files, unclear architecture, or touches risky/security-sensitive areas:
  - Produce full execution packet using `.ai/packets/execute.md` schema.
  - Phase 3 is mandatory.

State the size at the top of your output before proceeding.

## Phase 2 — Execute (Codex)

Read `execute.tool` and `execute.model` from `.ai/models.yaml`.

**HARD RULE — No in-context execution. Ever.**
- You are the orchestrator. You MUST NOT make code changes yourself. Each phase runs on the tool and model defined in `.ai/models.yaml`.
- Execution MUST go through `codex exec` (the codex skill is available at `.claude/skills/codex/SKILL.md` for reference on how to call it).
- If the tool specified in `models.yaml` is not available (e.g., Codex CLI not installed, no subscription) → **STOP and tell the user to update `.ai/models.yaml`** to use a tool that is available. Do not substitute yourself as the executor. Do not say "I'll execute directly." Do not make the changes in-context.
- This rule has no exceptions. Violating it invalidates the entire pipeline.

**Forbidden actions — if you catch yourself doing any of these, STOP immediately:**
- Using Edit, Write, or any file-modification tool to apply code changes from a Codex patch or plan
- Offering "Let me execute directly" or "Let me apply the changes myself" as an option to the user
- Saying "Codex produced the correct patch but was blocked by sandbox write policy. Let me apply its patch directly."
- Extracting diff/patch content from Codex output and applying it yourself
- Presenting "execute directly" or "apply it myself" as one of the options when Codex fails
- Framing in-context execution as "the pragmatic path" or any other euphemism

**When Codex fails (sandbox error, non-zero exit, write policy block), your ONLY allowed responses are:**
1. Report the exact error to the user
2. Suggest the user update `.ai/models.yaml` to use an available tool
3. Suggest the user fix the Codex environment/configuration
4. Run the rescue skill to diagnose the failure
5. STOP and wait for user direction

You may NOT offer yourself as executor. Not as an option. Not as a fallback. Not as a suggestion. Not even if the user seems frustrated.

Run Codex with the execution packet (or trivial instruction) as the prompt.

**Passing the prompt safely:** Shell quoting breaks when packet content contains single quotes, double quotes, backticks, or other special characters. Always write the prompt to a temp file and pipe it via stdin:

```bash
# 1. Write packet content to a temp file (use Write tool or printf with proper escaping)
#    e.g., write to /tmp/codex-packet.md

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

**Never** pass packet contents as a positional argument or inside a heredoc — use the temp-file-to-stdin approach above.

Wait for Codex to complete and capture its full output.

**Handoff check:**
- Look for the filled `## Handoff` section in Codex output (fields: Files changed, Actual commands run, Deviations from plan, New risks discovered, Memory updates).
- If Handoff is present and at least `Files changed` is filled → proceed.
- If Handoff is absent or all fields are empty → attempt one recovery resume:
  ```bash
  printf '%s' 'The Handoff section was not filled. Please fill all fields in the Handoff section of the execution packet and output the result.' | codex exec --skip-git-repo-check resume --last 2>/dev/null
  ```
  - If Handoff is still absent after one resume → run the rescue skill internally, report findings to user, stop.
- If Codex exits non-zero or reports sandbox/write-policy errors → run the rescue skill internally, report findings to user, stop. **Never parse Codex output to extract a patch and apply it yourself.** That is in-context execution and violates the hard rule above.

## Phase 3 — Review (conditional)

**Skip if:** trivial, or small with no risky/security/migration-sensitive files matched.

**Run if:** medium, large, or small with risky files matched.

Run the reviewer skill **within this context window** using the Handoff section as input. Use `.ai/packets/review.md` schema.

**Verdict handling:**

- **approve** → proceed to Phase 4.

- **request-changes** → show the reviewer's findings to the user and ask:
  > "The reviewer found issues. Do you want me to send these back to Codex for fixes? (yes/no)"

  - If **yes** → resume Codex (no config flags on resume). Write the resume prompt to a temp file first to avoid quoting issues, then pipe it:
    ```bash
    # Write resume prompt to temp file using the Write tool (not heredoc/echo)
    # Content: "Reviewer findings:\n<findings>\n\nOriginal objective: <objective>"
    cat /tmp/codex-resume.md | codex exec --skip-git-repo-check resume --last 2>/dev/null
    rm -f /tmp/codex-resume.md
    ```
    Re-run the reviewer on the new Handoff.
    - If verdict is still `request-changes` → stop. Report full reviewer findings and Handoff to user. Do not loop further.
    - If verdict is `approve` → proceed to Phase 4.

  - If **no** → stop. Report reviewer findings to user as-is.

- **escalate** → stop. Report full reviewer findings and Handoff context to user.

## Phase 4 — Wrap up

1. **Pending deletions:** Check the Handoff `Pending deletions` field.
   - If non-empty → show the list to the user and ask:
     > "The executor flagged these files/dirs for deletion. Confirm to proceed: [list]"
   - If confirmed → execute each deletion.
   - If declined → report them as unresolved and skip.

2. Collect memory updates:
   - From executor Handoff: `Memory updates` field.
   - From reviewer output: `Memory updates to apply` field (if review ran).
2. If any memory updates exist → run the maintenance skill **within this context window** to append them to `.ai/memory.md`.
3. Report to user:
   - **Summary:** what was done (1-3 sentences)
   - **Files changed:** list from Handoff
   - **Validation:** commands run and result
   - **Risks:** any risks noted by executor or reviewer
   - **Memory updates applied:** list or "none"

## Error table

| Situation | Action |
|-----------|--------|
| `models.yaml` missing | STOP — tell user to run `install.sh` |
| `project_name` is `unknown` | STOP — tell user to run bootstrap skill |
| `call-claude` skill missing at `~/.agents/skills/call-claude/` | STOP — tell user to run `install.sh` |
| Tool from `models.yaml` unavailable (e.g., Codex not installed) | STOP — tell user to update `.ai/models.yaml` to use an available tool. **Never execute in-context as fallback.** |
| Codex exits non-zero | Run rescue skill internally, report error + allowed options (see Phase 2), stop. **Never extract and apply patches yourself. Never offer to execute directly.** |
| Codex blocked by sandbox/write policy | Same as non-zero exit. Report the error and the 5 allowed responses from Phase 2. Do NOT apply the patch in-context. Do NOT offer "let me do it" as an option. |
| Handoff absent after one resume | Run rescue skill internally, report, stop |
| Reviewer `request-changes` twice | Stop, report full context to user |
| Reviewer `escalate` | Stop, report full context to user |

## Notes

- The `plan` and `review` entries in `models.yaml` are informational only for this skill. Planning and reviewing always run in the current Claude session. To use a different model for review, invoke the reviewer skill manually in a separate Claude session.
- Never pass config flags (`-m`, `--config`, `--sandbox`, `--full-auto`) when using `resume --last`. Only the prompt is passed.
- The retry cap for `request-changes` is exactly 1. Do not loop beyond that.
