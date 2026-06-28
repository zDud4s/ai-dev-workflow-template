# Dispatch mechanism

Shared rules for any controller (currently the orchestrator) that dispatches workflow phases through configured tools and models. The pipeline itself lives in the `orchestrate` skill (`.claude/skills/orchestrate/SKILL.md` for Claude, `~/.agents/skills/orchestrate/SKILL.md` for Codex); everything here is the mechanical layer underneath it.

## Dispatch contract

A controller never substitutes its own model for a configured workflow phase. For every phase (`plan`, `execute`, `review`, `rescue`, `maintenance`):

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

**Synchronous-call invariant.** Dispatcher subprocesses MUST be launched synchronously (the controller blocks on stdout/stderr until the process exits or the timeout fires). NEVER use background-launch flags — e.g. Claude Code's `Bash` tool with `run_in_background: true`, shell `&`, `nohup`, `Start-Job`, `Start-Process` without `-Wait`. Background launches return immediately with metadata (PID / output-file path) instead of the phase's actual output, breaking the Handoff check, the dashboard's dispatch tracker, and the metrics-logging step (no exit code, no duration, no stdout to parse). If a phase is genuinely too long for the default timeout, raise `<phase>.timeout_seconds` in `.ai/models.yaml`; do not work around it with background mode.

**Mode: inline.** Run the phase logic in the current session. Assemble the same full prompt you would send to a subprocess, then follow it directly. If `mode: inline` is set explicitly and session model differs from `phase.model`, warn first:

> "Warning: Phase `<phase>` is set to inline but session model (`<session.model>`) differs from phase model (`<phase.model>`). Running in session model."

### Mode: agent (in-process)

The controller delegates via the Claude Code Task tool — an in-process subagent that shares the controller's sandbox but runs in its own context window. No subprocess, no temp file, no `timeout` wrapper, no `rm`.

Dispatch: call the Task tool with `subagent_type="general-purpose"`, `model=<family>` where `<family>` is the `sonnet | opus | haiku` prefix of `<phase.model>` (intra-family version differences collapse — e.g. `claude-sonnet-4-6` and `claude-sonnet-4-5-20241022` both resolve to `sonnet`). Exact-version routing requires `mode: dispatcher`. Pass the full assembled prompt as the `prompt` parameter.

The Task tool returns the phase output directly. Apply the same exit/escalation handling as for subprocess modes (escalation block → structured failure; empty output → silent failure).

### Mode: dispatcher (subprocess)

Wrap every subprocess call with `timeout <T>s` where `<T>` is `<phase>.timeout_seconds` from `.ai/models.yaml`, defaulting to 600s for plan/review/maintenance/rescue/bootstrap and 1800s for execute. After dispatching, `rm -f /tmp/phase-<phase>-prompt.md`.

- **Target = `claude`** (`dispatcher` when `<phase>.tool == claude`):
  ```bash
  timeout <T>s sh -c 'cat /tmp/phase-<phase>-prompt.md | claude -p --bare --exclude-dynamic-system-prompt-sections "Execute the attached <phase> phase exactly. Return only the phase result. If you cannot proceed, emit the Escalation output format and exit non-zero." --model <phase.model> [--effort <phase.reasoning_effort>] --output-format json 2>/dev/null'
  ```
  The controller reads `.result` from the JSON object as the phase output (instead of raw stdout) and maps `.usage.input_tokens` -> `tokens_in`, `.usage.output_tokens` -> `tokens_out`, `.usage.cache_read_input_tokens` -> `cache_read`, and `.usage.cache_creation_input_tokens` -> `cache_creation`.
  `--bare` skips CLAUDE.md auto-discovery, hooks, plugin sync, auto-memory, keychain — the phase only sees the prompt we pipe. `--exclude-dynamic-system-prompt-sections` moves per-machine cwd/env/git/memory out of the system prompt into the first user message; the stable prefix hits the Anthropic prompt cache (5-min TTL) on repeat calls. `<phase.reasoning_effort>` ∈ {`low`, `medium`, `high`, `xhigh`, `max`} from `.ai/models.yaml`; omit the flag entirely when the field is absent (claude default applies).

- **Target = `codex`** (`dispatcher` mode when `<phase>.tool == codex`):
  ```bash
  timeout <T>s sh -c 'cat /tmp/phase-<phase>-prompt.md | codex exec --skip-git-repo-check -m <phase.model> --config model_reasoning_effort="<phase.reasoning_effort>" -C <absolute project path> 2>/tmp/phase-<phase>-stderr.log'
  ```
  The controller parses `/tmp/phase-<phase>-stderr.log` for `tokens used[:\s]+([\d,]+)`, strips commas from the captured number, records it as `tokens_out`, then runs `rm -f /tmp/phase-<phase>-stderr.log`. Codex prints a single total, so only `tokens_out` is populated (`tokens_in` stays null).
  `<phase.reasoning_effort>` ∈ {`xhigh`, `high`, `medium`, `low`} from `.ai/models.yaml`; default `medium` if absent. Codex does NOT accept `max` — only claude does. **Any write-capable codex phase** (a phase whose packet edits repo files — `execute`, `rescue`, `maintenance`, `bootstrap`) additionally appends `--dangerously-bypass-approvals-and-sandbox` — without it, codex's own sandbox raises approval prompts that stall the subprocess silently. Read-only phases (`plan`, `review`) omit the flag.

  **Controller sandbox bypass.** The bypass flag only disables *codex's* sandbox. When the controller is Claude Code with its own sandbox enabled, the dispatched codex subprocess still runs inside the controller's sandbox, which blocks codex's writes/network even though the command is permission-allowed. A write-capable codex dispatch MUST therefore run outside the controller's sandbox: either via `sandbox.excludedCommands` (declarative, in `.claude/settings.json`) or by issuing the dispatch `Bash` call with `dangerouslyDisableSandbox: true`. Both require `sandbox.allowUnsandboxedCommands` to stay `true` (the default).

Subprocess exit handling lives in the dispatch error table below (exit 0, non-zero ± `## Escalation`, exit 124 timeout, empty-output exit 0).

## Cache-stable prompt layout

When assembling a dispatched-phase prompt, order the sections so that content shared across sequential phases of the same task forms a byte-identical prefix, maximizing Anthropic prompt-cache hits (5-min TTL).

**Stable prefix** (same across phases of one task — order matters):

1. Phase skill body (from discovery path)
2. Packet schema (from `.ai/packets/`)
3. `project.yaml` header fields

**Volatile suffix** (changes per phase or per dispatch):

4. Task / current objective
5. Memory slice (filtered by planner tags)
6. Prior-phase artifacts (Handoff output, reviewer findings, etc.)

Sequential phases of the same task MUST emit a byte-identical prefix; volatile content goes at the end.

## Resume rule

When resuming an executor session (`codex exec resume --last`), pass only the prompt. Never pass config flags (`-m`, `--config`, `--sandbox`, `--full-auto`) on resume — they will be rejected or ignored inconsistently. The same applies to `--dangerously-bypass-approvals-and-sandbox`: a resume cannot carry it, so a resumed write-capable run stalls on codex's sandbox approvals and won't match the permission allow rule. A review send-back (which must edit files) therefore uses a fresh write-capable `execute` dispatch — the full dispatcher command with the bypass flag — not a resume.

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
