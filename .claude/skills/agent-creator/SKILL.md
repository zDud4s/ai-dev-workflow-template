---
name: agent-creator
description: Create project-owned agent files (`.claude/agents/<name>.md`). Use when the user explicitly asks to create, add, scaffold, draft, or design a new agent, or when repeated conversation patterns or `git log` activity suggest the same workflow keeps being handled manually and should become an agent. Detect repetition, propose a concrete spec, preview the canonical template, write only after approval, and then suggest an `agent-improver` audit.
tools: Read, Bash, Write
---

# Agent Creator

Create project-owned agent files (`.claude/agents/<name>.md`) when the user explicitly asks for a new agent or when repeated conversation and commit patterns show the same workflow keeps being handled manually. Detect repetition, propose a concrete spec, preview the canonical template, write only after approval, and then suggest an `agent-improver` audit.

## When this skill writes

This skill writes only after the user has explicitly approved the exact new agent to create, and only to `.claude/agents/<name>.md` in the current project. The `<name>` must be lowercase, hyphenated, and derived from the approved spec.

Use `Write` only for new files under `.claude/agents/`. Never write user-scope agents, plugin agents, workflow files, mirrored `.agents/skills/` files, or existing agent files. If the target file already exists, stop and suggest using `agent-improver` instead.

## Workflow

### Phase 1: Detect repetition

Use the active conversation/session context plus recent commits to identify whether a dedicated agent is justified. Always run:

```bash
git log --oneline -n 50
```

Look for repeated manual operations, repeated delegation to a broad agent, recurring phrases from the user, or a direct request such as "create an agent for this". If the user made an explicit creation request, continue even if the history signal is weak. If both the explicit request and repetition signal are absent, say there is not enough evidence and ask what agent they want.

### Phase 2: Propose the spec

Before writing anything, propose a short spec with:

| Field | Value |
|---|---|
| Name | `<lowercase-hyphenated-name>` |
| Purpose | `<one sentence>` |
| Trigger phrasing | `<explicit and implicit trigger examples>` |
| Tools | `<least-privilege tool list>` |
| Model | `<haiku / sonnet / opus / inherit>` |
| Scope | `project: .claude/agents/<name>.md` |

Ask the user to approve, revise, or reject the spec. Silence is not approval.

### Phase 3: Preview the file shape

Read [`references/agent-template.md`](references/agent-template.md) and summarize the matching structure the new agent will use: frontmatter, persona line, numbered responsibilities, detailed process, quality standards, output format, and edge cases. Do not inline the full reference unless the user asks.

### Phase 4: Write on approval

After approval, write exactly one new file: `.claude/agents/<name>.md`. The file must follow the approved spec and the template shape. Use an explicit `tools:` allowlist and include enough trigger examples in the description for reliable invocation.

If `.claude/agents/` does not exist, create the directory only as part of writing the approved agent file. If the target file exists, do not overwrite it.

### Phase 5: Suggest improver audit

After writing the file, suggest running the project `agent-improver` skill to audit the new agent's trigger reliability, tool allowlist, and structure. Do not invoke it automatically unless the user asks.

## Output Template

```markdown
## Agent Creation Proposal

| Field | Value |
|---|---|
| Name | `<name>` |
| Purpose | `<purpose>` |
| Trigger phrasing | `<phrases>` |
| Tools | `<tools>` |
| Model | `<model>` |
| Scope | `.claude/agents/<name>.md` |

Template match:
- Frontmatter: ...
- Body structure: ...
- Output format: ...
- Edge cases: ...

Approval needed:
Reply with "approve <name>" to create `.claude/agents/<name>.md`, or list changes to the spec.
```

After writing:

```markdown
Created `.claude/agents/<name>.md`.

Suggested next step: run `agent-improver` against the new agent before relying on it heavily.
```

## Boundaries

| Boundary | Rule |
|---|---|
| Allowed writes | `.claude/agents/<name>.md` only |
| Required approval | User must approve the exact spec and target path before `Write` |
| Existing files | Do not overwrite; suggest `agent-improver` for edits |
| Plugin agents | Read-only; never create, edit, or mirror plugin agent files |
| Workflow core | Do not modify `.ai/workflow/`, `.ai/packets/`, install scripts, settings, or project memory |
| Sibling skill | Use `agent-improver` when the task is to audit or improve an existing agent |
