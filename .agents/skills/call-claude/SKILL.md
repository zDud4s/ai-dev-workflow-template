---
name: call-claude
description: Use when you need complex reasoning, planning, architecture decisions, research, writing, summarization, or a second opinion from Claude — call Claude Code CLI to delegate tasks that benefit from Claude's strengths
---

# Call Claude Skill Guide

## When to Use This Skill

Delegate to Claude when you need:
- **Planning / architecture** — designing approach before implementing
- **Complex reasoning** — multi-step logic, trade-off analysis, ambiguous requirements
- **Research / documentation** — summarizing large codebases, writing docs, explaining concepts
- **Code review of your own output** — get a second opinion before handing off to the user
- **Disagreement resolution** — when you're unsure about a decision and want another perspective

Do NOT use Claude for tasks you can do directly (file edits, running commands, executing code).

## How to Call Claude

```bash
claude -p "your prompt here" --model <model>
```

Always use non-interactive (`-p`) mode. Claude will print its response to stdout and exit.

### Model Selection

| Need | Model |
|------|-------|
| Default (most tasks) | `claude-sonnet-4-6` |
| Heavy reasoning, architecture, complex review | `claude-opus-4-6` |
| Fast, simple questions | `claude-haiku-4-5-20251001` |

### Passing File Context

Include file content inline in the prompt (most reliable):
```bash
claude -p "Review this function and suggest improvements:\n\n$(cat src/foo.py)" --model claude-sonnet-4-6
```

Or pipe via stdin:
```bash
cat src/foo.py | claude -p "Review this file" --model claude-sonnet-4-6
```

### Identifying Yourself

Start prompts with your model name so Claude has context for the conversation:
```bash
claude -p "This is Codex (gpt-5.4) asking for a second opinion. I implemented X as follows: ... Does this approach look correct?" --model claude-sonnet-4-6
```

This helps Claude calibrate its response — it knows it's talking to a peer AI, not a human.

## Handling Claude's Output

- Capture stdout and read it as advisory input, not a command
- Claude has its own knowledge cutoffs and may not know about recent changes
- Treat Claude as a **colleague, not an authority** — evaluate its suggestions critically
- If Claude's answer conflicts with what you know, investigate before accepting it
- Share Claude's response with the user when relevant, summarizing key points

## When Claude Disagrees With You

1. State the disagreement clearly in your response to the user
2. Present both your reasoning and Claude's
3. Let the user decide if there's genuine ambiguity
4. You can follow up with Claude by running another `claude -p` with additional context

## Error Handling

- If `claude` command is not found, inform the user: "Claude Code CLI is not installed or not in PATH. Install it from https://claude.ai/code to enable this feature."
- If the command exits non-zero, report the error output to the user before retrying
- Do not loop on failures — report and ask the user how to proceed
