# Dispatch mechanism

Shared rules for any controller (currently the orchestrator) that dispatches workflow phases through configured tools and models. The pipeline itself lives in the `orchestrate` skill (`.claude/skills/orchestrate/SKILL.md` for Claude, `~/.agents/skills/orchestrate/SKILL.md` for Codex); everything here is the mechanical layer underneath it.

## Dispatch contract

A controller never substitutes its own model for a configured workflow phase. For every phase (`plan`, `execute`, `review`, `rescue`, `maintenance`, `bootstrap`):

1. Read `<phase>.tool` and `<phase>.model` from `.ai/models.yaml`.
2. Build a standalone prompt packet for that phase, containing:
   - relevant skill instructions from the `<phase>` skill, resolved via the controller's discovery path (`.claude/skills/<phase>/SKILL.md` for Claude, `~/.agents/skills/<phase>/SKILL.md` for Codex) when available
   - the current objective
   - the required repo context
   - the relevant packet schema from `.ai/packets/`
3. Execute the phase through the configured tool/model.
4. Capture and retain: phase name, requested tool, requested model, actual command used, exit status.
5. If the configured tool is unavailable, STOP and tell the user to update `.ai/models.yaml` or fix the tool. Never fall back to the current session's model.

A phase runs in the current context window only if routing resolves to `inline` (see below).

## Dispatch routing

Each phase resolves to one of three execution modes: `inline`, `agent`, or `dispatcher`.

When `dispatch_mode: auto` (and no explicit per-phase `mode` override):

| Comparison | Mode |
|---|---|
| `phase.tool != session.tool` | dispatcher |
| `phase.tool == session.tool`, models differ | agent |
| `phase.tool == session.tool`, models match | inline |

Override / fallback table:

| `dispatch_mode` | phase `mode` field | Result |
|---|---|---|
| `auto` | not set | computed from session comparison |
| `auto` | set | explicit `mode` overrides auto |
| `manual` | set | use explicit `mode` |
| `manual` | not set | default to `dispatcher` |

Model comparison is exact-string. Unrecognized `session.model` values pass through; the tool validates them.

Example explicit override:

```yaml
review:
  tool: claude
  model: claude-opus-4-6
  mode: agent
```

## Non-interactive invariant

Subprocesses (modes `agent` and `dispatcher`) run with no interactive channel to the user. The user only sees output AFTER the subprocess completes. Dispatched phases therefore MUST:

- Never prompt the user. No "yes/no", no "confirm", no "press enter".
- Never block on stdin past the initial prompt that the controller pipes in.
- Emit a final, self-contained answer (success or structured failure — see "Escalation output format" below) and exit.

The controller is the only place where user-facing questions live. If a dispatched phase needs human input mid-task, it must terminate with an escalation block; the controller then surfaces it to the user and decides what to do.

Inline mode does not have this restriction — it runs in the controller's session, which is the user's session. Questions there reach the user normally.

## Escalation output format

When a dispatched phase cannot proceed (ambiguous packet, missing context, blocked by environment, needs a decision), it MUST emit this block as its final output and exit non-zero:

```
## Escalation
reason: <one-line cause>
needed: <what the user / orchestrator must decide or provide>
suggested-next: <one concrete option, or "none">
partial-output: <what was produced before stopping, or "none">
```

The controller treats any non-zero exit with an `## Escalation` block as a structured failure (not a crash) and routes it to the user with the four fields verbatim. No `## Escalation` block + non-zero exit = treat as crash / sandbox / tooling failure.

## Dispatcher rules

The subprocess does not inherit the controller's session. Do not send bare instructions like "Use the planner skill." Always inline the full prompt — skill instructions, task context, and packet schema — into the delegated input.

**Prompt-passing convention.** Shell quoting breaks on packet content containing quotes, backticks, or other special characters. Always write the prompt to a temp file and pipe via stdin. Never use positional arguments or heredocs.

**Timeout convention.** Every subprocess call MUST have a wall-clock timeout — without it, a hung subprocess freezes the whole pipeline silently. Default: 600s (10 min) for `plan` / `review` / `maintenance` / `rescue` / `bootstrap`; 1800s (30 min) for `execute`. Override per phase via `<phase>.timeout_seconds` in `.ai/models.yaml` if a task genuinely needs longer.

Wrap each dispatcher command with a timeout. On POSIX shells: `timeout <N>s <cmd>`. On Windows / PowerShell: `Start-Process -Wait -Timeout` or a wrapper script. Timeout exit (124 on POSIX `timeout`) is treated as a freeze: dispatch rescue, report to user, stop.

**Mode: inline.** Execute the phase logic in the current session. Assemble the same full prompt you would send to a subprocess, but follow it directly. If `mode: inline` is set explicitly and the session model differs from `phase.model`, warn first:

> "Warning: Phase `<phase>` is set to inline but session model (`<session.model>`) differs from phase model (`<phase.model>`). Running in session model."

**Mode: agent** (same tool, different model):

```bash
timeout <T>s sh -c 'cat /tmp/phase-<phase>-prompt.md | claude -p --bare --exclude-dynamic-system-prompt-sections "Execute the attached <phase> phase exactly. Return only the phase result. If you cannot proceed, emit the Escalation output format and exit non-zero." --model <phase.model> 2>/dev/null'
```

**Mode: dispatcher** (different tool):

- If `<phase>.tool` is `claude`:
  ```bash
  timeout <T>s sh -c 'cat /tmp/phase-<phase>-prompt.md | claude -p --bare --exclude-dynamic-system-prompt-sections "Execute the attached <phase> phase exactly. Return only the phase result. If you cannot proceed, emit the Escalation output format and exit non-zero." --model <phase.model> 2>/dev/null'
  ```
- If `<phase>.tool` is `codex`:
  ```bash
  timeout <T>s sh -c 'cat /tmp/phase-<phase>-prompt.md | codex exec --skip-git-repo-check \
    -m <phase.model> \
    --config model_reasoning_effort="<phase.reasoning_effort>" \
    -C <absolute project path> \
    2>/dev/null'
  ```

  `<phase.reasoning_effort>` is read from `.ai/models.yaml` (`<phase>.reasoning_effort`). Valid values match the codex skill: `xhigh`, `high`, `medium`, `low`. If the field is absent, default to `medium`.

`<T>` comes from `<phase>.timeout_seconds` in `.ai/models.yaml`, falling back to the per-phase defaults (600s for plan/review/maintenance/rescue/bootstrap; 1800s for execute).

The `execute` phase specifically adds `--dangerously-bypass-approvals-and-sandbox` when dispatching to codex — without it, sandbox approval prompts can stall the subprocess silently. All other phases run without write access (read-only operations should not trigger approval prompts; if they do on your platform, set the appropriate read-only bypass for that tool).

**Cache-friendly flags for claude.** Both `agent` and `dispatcher` modes use `--bare` (skips CLAUDE.md auto-discovery, hooks, plugin sync, auto-memory, keychain — the phase only sees the prompt we pipe) and `--exclude-dynamic-system-prompt-sections` (moves per-machine cwd/env/git/memory paths out of the system prompt into the first user message, so the system-prompt prefix is stable across calls and hits the Anthropic prompt cache, 5-min TTL). On repeat calls within the cache window (reviewer iterations, multi-chunk runs), this turns the system-prompt portion into a cheap cache read.

After dispatching, clean up the temp file (`rm -f /tmp/phase-<phase>-prompt.md`).

Subprocess exit handling:
- Exit 0 → success, parse phase output.
- Exit non-zero WITH `## Escalation` block in output → structured escalation, follow the per-phase error policy.
- Exit non-zero WITHOUT escalation block → treat as crash/sandbox/tooling failure, follow the per-phase error policy.
- Exit 124 (POSIX `timeout` killed the process) → treat as freeze: dispatch rescue, report to user, stop. Do not retry without a fix to the timeout setting or the upstream cause.

## Resume rule

When resuming an executor session (`codex exec resume --last`), pass only the prompt. Never pass config flags (`-m`, `--config`, `--sandbox`, `--full-auto`) on resume — they will be rejected or ignored inconsistently.

## Dispatch error table

Errors raised by the dispatch mechanism itself, before any phase logic runs. Pipeline-specific errors (planner output shape, Handoff completeness, reviewer verdicts) live in the orchestrate skill.

| Situation | Action |
|---|---|
| `.ai/models.yaml` missing | STOP — tell user to run `install.sh` |
| `project_name` in `project.yaml` is `unknown` | STOP — tell user to run bootstrap skill |
| Executor skill for the configured `<execute.tool>` missing in the controller's discovery path | STOP — tell user to run `install.sh` (or `update-workflow.sh`) so the orchestrator and executor skills are installed |
| Tool from `models.yaml` unavailable | STOP — tell user to update `.ai/models.yaml`. Never execute in-context as fallback. |
| `dispatch_mode: auto` but `session` block missing or partial | STOP — "Auto dispatch requires a complete `session` block in `.ai/models.yaml` with both `session.tool` and `session.model`." |
| `dispatch_mode` has an unrecognized value | STOP — "`dispatch_mode` must be `auto` or `manual`. Got: `<value>`." |
| `session.tool` has an unrecognized value | STOP — "`session.tool` must be one of: `claude`, `codex`. Got: `<value>`." |
| `mode: inline` override where session model differs from phase model | Warn (see above), proceed |
| `dispatch_mode` field absent | Treat as `manual`. All phases default to `dispatcher` unless an explicit per-phase `mode` is set. |
| Subprocess hit timeout (exit 124 / wrapper kill) | Treat as freeze. Dispatch rescue with timeout context, report to user, stop. Never auto-retry the same command — fix the timeout or the upstream cause first. |
| Non-zero exit with `## Escalation` block | Structured escalation. Surface the four fields to the user verbatim; do not retry the phase blindly. |
| Non-zero exit without `## Escalation` block | Treat as crash / sandbox / tooling failure. Follow the per-phase error policy. |
| Subprocess produced empty output and exit 0 | Treat as silent failure. Report to user and stop — do not assume success. |
| `auto_select.enabled: true` but `## Selected models` block missing in planner output | STOP — "invalid planner output: Selected models block missing" |
| `auto_select.enabled: true` but `## Selected models` block malformed | STOP — "invalid planner output: Selected models block malformed: <phase or 'header'>" |
| Planner-selected tool not locally available | STOP — "auto-selected tool unavailable: <tool> for phase <phase>; fix via .ai/models.yaml fallback or install the tool". Never silently fall back to `models.yaml`. |
