---
name: agent-improver
description: Audit and improve agent files (`.claude/agents/*.md`). Use when the user asks to check, audit, review, score, or improve agents, mentions "agent maintenance" or "agent quality", wonders which agents are missing or duplicated, wants to tighten tool allowlists, or wants to know if their agent descriptions trigger reliably. Scans project + user agents, scores against a quality rubric, outputs a report, then applies targeted edits after approval. Optionally suggests new agents to create and delegates creation to the project `agent-creator` skill.
tools: Read, Glob, Grep, Bash, Edit
---

# Agent Improver

Audit and improve agent files (`.claude/agents/*.md`) across project and user scopes. Scan all agents, score editable ones against a quality rubric, output a report, then apply targeted edits after the user approves. Optionally suggest new agents to fill gaps and delegate creation to the project `agent-creator` skill.

> **Tool rationale (least-privilege check this skill applies to itself):** `Read`/`Glob`/`Grep` for discovery and inspection; `Edit` for targeted updates (never `Write`); `Bash` only for `git log` inside Suggest-new-agents mode. New-agent creation is delegated to the project `agent-creator` skill via the Skill tool (always available — no allowlist entry needed).

## When this skill writes

This skill writes to agent files **only after the user has explicitly approved changes**, and **only under `.claude/agents/` (project) or `~/.claude/agents/` (user)**. Plugin-scope agents under `~/.claude/plugins/marketplaces/**` and `~/.claude/plugins/cache/**` are **read-only** — never edit them, never score them as candidates for in-place fixes, and never propose direct edits to them. If a plugin agent has issues, surface them as commentary only.

Use `Edit` for every change (never `Write`) so the existing structure of each agent file is preserved.

## Workflow

### Phase 1: Discovery

Glob the three scopes and emit the catalogue table **inline in the response so the user can see what will be assessed before Phase 2 runs**. Mark each agent with its scope and editability.

Tool calls:

- Project (editable): `Glob` pattern `.claude/agents/*.md`
- User (editable): `Glob` pattern `~/.claude/agents/*.md`
- Plugins (read-only):
  - `Glob` pattern `~/.claude/plugins/marketplaces/**/agents/*.md`
  - `Glob` pattern `~/.claude/plugins/cache/**/agents/*.md`

Emit a catalogue:

| Agent | Scope | Editable | Path |
|---|---|---|---|
| `<name>` | project / user / plugin | yes / no | `<absolute or repo-relative path>` |

If a name appears in more than one scope, flag the row as a potential duplicate so Phase 2 can investigate.

### Phase 2: Quality assessment

For each **editable** agent (project + user scopes only), score against the rubric in [`references/quality-criteria.md`](references/quality-criteria.md). The rubric has six criteria that sum to 100. Do not inline the rubric here — read the reference file and apply it.

**Skip plugin-scope agents entirely.** They are read-only; scoring them implies we might edit them, which we will not. List them in the report under "Plugin agents (read-only)" with a one-line summary only.

For each editable agent, gather:

- The `name`, `description`, `tools`, and `model` from YAML frontmatter (use `Read` to load the file head).
- The body of the system prompt (rest of the file).
- Any `<example>` blocks inside the description.

Then assign a score per criterion and a total `NN/100`. Use letter grades: A 90+, B 75–89, C 60–74, D 40–59, F <40.

### Phase 3: Quality report

**Always run this phase before any edit.** Output the report in the exact format shown in the "Report Format" section below. Do not skip sections; if a scope has zero agents, emit the section header with "(none)".

**Empty editable scopes:** If both project AND user scopes have zero agents, there is nothing to confirm in Phase 4 — skip directly to offering Suggest-new-agents mode (see below). Still emit the Plugin agents section as commentary.

### Phase 4: Confirm

After emitting the report, ask the user explicitly:

> Apply these changes? (you can pick a subset)

…or equivalent wording that lets the user pick a subset. List each recommended change with an index so the user can reply "apply 1, 3, 5" or "all" or "none". **Never edit without an affirmative answer covering each file you will touch.** Silence is not consent.

### Phase 5: Apply

For each approved change, use `Edit` (never `Write`) so existing structure is preserved. Apply one change at a time and read the file back into context before the next edit if surrounding content matters. **Never touch paths under `~/.claude/plugins/`.** If a planned edit would land in a plugin path, refuse and explain.

After all edits, emit a short confirmation listing the files changed and a one-line summary per change.

## Report Format

````markdown
## Agent Quality Report

### Summary
- Agents found (project): X
- Agents found (user): Y
- Plugin agents (read-only, for cross-check): Z
- Average score: NN/100
- Agents needing update: K

### Project agents

#### 1. `.claude/agents/<name>.md`
**Score: NN/100 (Grade: X)**

| Criterion | Score | Notes |
|---|---:|---|
| Description quality | nn/25 | ... |
| System prompt structure | nn/25 | ... |
| Tool allowlist | nn/15 | ... |
| Model choice fit | nn/10 | ... |
| Distinctiveness | nn/15 | ... |
| Currency | nn/10 | ... |

**Issues:**
- ...

**Recommended changes:**
- ...

### User agents
(same shape as Project agents)

### Plugin agents (read-only)
- List of (name, source plugin, one-line summary). No scores.

### Suggested new agents
(Shown only when the user opted into suggestion mode, or when high-confidence duplicates can be consolidated.)
````

> **Placeholder legend for the template above:** `X`, `Y`, `Z`, `K` in the Summary block are literal counts; `NN` is the total score 0–100; `nn` is a per-criterion score in that criterion's range (e.g. `nn/25` for a criterion worth 25 pts); the letter `X` inside `(Grade: X)` is overloaded — there it is one of A/B/C/D/F, not a count. Context disambiguates: integer in `Summary`, letter inside `(Grade: …)`. The case difference (`nn` vs `NN`) is intentional to make the per-criterion vs total distinction visible at a glance.

## Update Strategy

Four kinds of updates. Apply only the ones the user approved in Phase 4.

### Description reinforcement

- If an agent's description has fewer than 2 `<example>` blocks, add examples that show realistic user phrasings and the assistant's `Task` dispatch to that agent.
- Sharpen trigger phrases: replace vague verbs ("help with", "deal with") with concrete actions ("audit", "refactor", "generate").
- Apply the **"pushy" pattern**: the description should cover both the **explicit ask** ("audit my agents") and the **implicit phrasings** ("which agents do I have", "are these triggering"). See [`references/agent-template.md`](references/agent-template.md) for examples.
- Never weaken triggers; only strengthen them. Under-triggering (the skill/agent is never invoked when it would be useful) is the primary failure mode for agent descriptions — strengthening is almost always the right direction.

### Tool-allowlist tightening

- Read the system prompt body and detect which tools it actually references or implies.
- Remove tools the prompt never references.
- Replace wildcard `*` with an explicit list when the role is narrow (e.g. a read-only auditor should not have `Write`).
- Show the change as a before/after diff:

  ```diff
  - tools: *
  + tools: Read, Glob, Grep, Bash
  ```

- **Never widen** the allowlist as part of "tightening." Widening is a separate, explicit change that requires its own approval line.

### System-prompt structure fixes

- Add missing sections without rewriting working prose. Common gaps:
  - Output format (what the agent returns to the caller)
  - Edge cases / refusal conditions
  - Numbered responsibilities or workflow phases
- If the prompt has no clear "You are X" persona line, add one at the top.
- Preserve existing wording inside sections; only add, do not paraphrase.

### Duplication flag

- **Never auto-deletes.** Surface duplicates as a recommendation only.
- For each duplicate pair, offer two options to the user:
  1. **Scope-narrow one side** — keep both but rewrite one description so triggers do not collide.
  2. **Deprecate one** — mark the weaker one for removal (the user removes it; this skill does not).
- Let the user choose; do not pick for them.

## Suggest-new-agents mode

This mode is **opt-in**. Trigger it only when the user asks something like:

- "What agents am I missing?"
- "Suggest new agents."
- "Which workflows should become agents?"

Process:

1. Use whatever conversation/session context is in the current turn (may be empty if invoked from a fresh session) plus recent commits via `Bash` (`git log --oneline -50`). Look for patterns: repeated delegation to `general-purpose`, repeated manual operations that look like a named workflow, recurring multi-step tasks the user always describes the same way. If both signals are empty, say so and ask the user what kind of agents they have in mind.
2. Cross-check candidates against the Phase 1 catalogue so no duplicates are proposed.
3. Output a `Suggested new agents` block:

   | Name sketch | Trigger phrasing | Why a dedicated agent helps | Confidence |
   |---|---|---|---|
   | `<name>` | "..." | ... | high / medium / low |

4. If the user approves any suggestion, **invoke the project `agent-creator` skill via the Skill tool** with a brief spec (name, purpose, trigger phrasings, tools, scope).

**This skill never writes new agent files itself.** Creation is `agent-creator`'s job.

## Boundaries

| Sibling | What it does | When to use it instead |
|---|---|---|
| `agent-creator` project skill | Creates new agents from a spec | When the user explicitly wants to create, not audit |
| `claude-md-improver` | Audits `CLAUDE.md` files | When the user mentions CLAUDE.md, not agents |
| `skill-creator` / `skill-reviewer` | Skills, not agents | When the artifact in question is a skill |
