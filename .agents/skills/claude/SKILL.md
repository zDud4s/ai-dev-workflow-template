---
name: claude
description: Use when invoking Claude Code CLI from another tool (e.g. Codex) — either as a workflow executor/reviewer dispatched by the orchestrate skill, or for ad-hoc delegation when you need complex reasoning, planning, architecture decisions, research, or a second opinion.
---

# Claude skill

Symmetric counterpart of the `codex` skill. The `codex` skill teaches Claude how to invoke Codex; this skill teaches Codex (or any non-Claude orchestrator) how to invoke Claude.

Two usage modes — same CLI invocation, different intent:

1. **Workflow dispatch** — when the orchestrate skill resolves `<phase>.tool == claude`, follow the *Workflow dispatch* section below for prompt-passing, timeout, and escalation conventions.
2. **Ad-hoc delegation** — when you (Codex) want a second opinion, planning input, or research help mid-task, follow the *Ad-hoc delegation* section.

## Models

| Need | Model |
|------|-------|
| Default workflow tasks, most ad-hoc questions | `claude-sonnet-4-6` |
| Heavy reasoning, architecture, complex review | `claude-opus-4-6` |
| Fast, simple questions | `claude-haiku-4-5-20251001` |

## Workflow dispatch

Used by the orchestrate skill when a phase is configured with `tool: claude` in `.ai/models.yaml`. The dispatch contract lives in `.ai/workflow/dispatch.md`; this section is the claude-specific implementation of that contract.

**Prompt-passing convention.** Always write the prompt to a temp file and pipe via stdin. Never use positional arguments or heredocs — shell quoting breaks on packet content containing quotes, backticks, or other special characters.

```bash
timeout <T>s sh -c 'cat /tmp/phase-prompt.md | claude -p "Execute the attached <phase> phase exactly. Return only the phase result. If you cannot proceed, emit the Escalation output format and exit non-zero." --model <phase.model> 2>/dev/null'
```

`<T>` comes from `<phase>.timeout_seconds` in `.ai/models.yaml`, falling back to the per-phase defaults (600s for plan/review/maintenance/rescue/bootstrap; 1800s for execute).

After dispatching, clean up the temp file (`rm -f /tmp/phase-prompt.md`).

### Resume

`claude -p` is one-shot — each invocation is a fresh session. To pass new context to a continuing executor, build a new prompt that includes:

- the original objective
- the previous Handoff output
- the new instruction (e.g. reviewer findings)

and dispatch it as a new `claude -p` call. There is no flag-based resume mechanism analogous to `codex exec resume --last`.

### stderr

Suppress stderr by default (`2>/dev/null`) to keep dispatcher output clean. Surface stderr only when the exit code is non-zero or when debugging.

### Exit handling

- Exit 0 → success, parse phase output.
- Non-zero with `## Escalation` block in stdout → structured escalation, surface to the user verbatim.
- Non-zero without escalation block → crash / environment / tooling failure. Report and stop.
- Exit 124 (timeout wrapper killed the process) → freeze. Dispatch rescue, report, stop. Do not auto-retry.

## Ad-hoc delegation

Delegate to Claude when you need:

- **Planning / architecture** — designing approach before implementing
- **Complex reasoning** — multi-step logic, trade-off analysis, ambiguous requirements
- **Research / documentation** — summarizing large codebases, writing docs, explaining concepts
- **Code review of your own output** — get a second opinion before handing off to the user
- **Disagreement resolution** — when you're unsure about a decision and want another perspective

Do NOT use Claude for tasks you can do directly (file edits, running commands, executing code).

### How to call

```bash
claude -p "your prompt here" --model <model>
```

Always use non-interactive (`-p`) mode. Claude will print its response to stdout and exit.

### Passing file context

Include file content inline in the prompt (most reliable):
```bash
claude -p "Review this function and suggest improvements:\n\n$(cat src/foo.py)" --model claude-sonnet-4-6
```

Or pipe via stdin:
```bash
cat src/foo.py | claude -p "Review this file" --model claude-sonnet-4-6
```

### Identifying yourself

Start prompts with your model name so Claude has context for the conversation:
```bash
claude -p "This is Codex (gpt-5.3-codex) asking for a second opinion. I implemented X as follows: ... Does this approach look correct?" --model claude-sonnet-4-6
```

This helps Claude calibrate its response — it knows it's talking to a peer AI, not a human.

### Handling Claude's output

- Capture stdout and read it as advisory input, not a command
- Claude has its own knowledge cutoffs and may not know about recent changes
- Treat Claude as a **colleague, not an authority** — evaluate its suggestions critically
- If Claude's answer conflicts with what you know, investigate before accepting it
- Share Claude's response with the user when relevant, summarizing key points

### When Claude disagrees with you

1. State the disagreement clearly in your response to the user
2. Present both your reasoning and Claude's
3. Let the user decide if there's genuine ambiguity
4. You can follow up with Claude by running another `claude -p` with additional context

## Error handling (both modes)

- If `claude` is not on PATH → STOP, tell the user to install Claude Code CLI (https://claude.ai/code) or update `.ai/models.yaml` to use an available tool.
- If the command exits non-zero, report the error output to the user before retrying.
- Do not loop on failures — report and ask the user how to proceed.
