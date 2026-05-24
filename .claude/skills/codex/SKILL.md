---
name: codex
description: Use when the user asks to run Codex CLI (codex exec, codex resume) or references OpenAI Codex for code analysis, refactoring, or automated editing
tools: Read, Bash, AskUserQuestion
---

# Codex Skill Guide

## Invocation modes

- **Controller (orchestrator-driven).** Prompt arrives via stdin from a controller. You MUST NOT prompt the user (no AskUserQuestion). Use values passed in the prompt verbatim. See "## Controller mode" below.
- **Direct (user-driven).** The user typed "run codex" or invoked you explicitly. Free to ask follow-ups. See "## Direct mode" below.

## Controller mode (called by orchestrator)

1. Required flags: `-m <model>`, `--config model_reasoning_effort="<level>"`, `--dangerously-bypass-approvals-and-sandbox` (for execute), `--skip-git-repo-check`, `-C <project-root>`, append `2>/dev/null` to suppress thinking tokens.
2. Prompt arrives via stdin from a temp file; do not pass as positional argument.
3. Resume: `cat /tmp/codex-resume.md | codex exec --skip-git-repo-check resume --last 2>/dev/null && rm -f /tmp/codex-resume.md`. No config flags on resume.
4. On any blocker emit a final `## Escalation` block (`reason:`, `needed:`, `suggested-next:`, `partial-output:`) and exit non-zero. See `.ai/packets/README.md` "Escalation block format" for the canonical shape.
5. On success emit the work followed by a complete `## Handoff` per `.ai/packets/execute.md` (validation evidence = one `$ cmd` / `exit:` / `tail: <last 5 lines>` block per validation command).

## Direct mode (user-invoked)

### Running a Task

Use `AskUserQuestion` once to ask for model (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, or `gpt-5.3-codex`) and reasoning effort (`xhigh`, `high`, `medium`, or `low`). Use `--skip-git-repo-check`, `-C <dir>` when needed, and append `2>/dev/null` unless debugging requires stderr.

For write-capable runs on Windows, prefer `--dangerously-bypass-approvals-and-sandbox`; ask permission first in direct mode. Pass the prompt via stdin from a temp file when quoting may be fragile.

### Following Up

After every direct `codex` command, use `AskUserQuestion` to confirm next steps, collect clarifications, or decide whether to resume. Restate the chosen model, reasoning effort, and sandbox mode when proposing follow-up actions.

Resume with a temp file: `cat /tmp/codex-resume.md | codex exec --skip-git-repo-check resume --last 2>/dev/null && rm -f /tmp/codex-resume.md`. The resumed session inherits the original model, reasoning effort, and sandbox mode unless the user explicitly changes them.

### Critical Evaluation of Codex Output

Treat Codex as a colleague, not an authority. Push back when output conflicts with known facts; verify recent APIs, model names, library behavior, and best practices against current docs or web research when needed.

When resuming to discuss a disagreement, identify yourself as Claude and include the evidence. Frame the disagreement as a peer review, then let the user decide when ambiguity remains.

### Error Handling

Stop and report failures whenever `codex --version` or `codex exec` exits non-zero. In direct mode, summarize stderr or partial output and use `AskUserQuestion` before retrying or changing flags.

Before high-impact flags such as `--full-auto`, `--sandbox danger-full-access`, or `--skip-git-repo-check`, ask permission unless the user already granted it. Warn clearly when Codex output is partial or uncertain.
