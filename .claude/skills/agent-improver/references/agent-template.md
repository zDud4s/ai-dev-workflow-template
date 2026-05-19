# Canonical agent template

This file describes the shape a high-quality agent file takes. Deviations from this template are not automatically bad — many strong agents omit a section or merge two — but **missing sections trigger structural-issue notes** during Phase 2 assessment in the `agent-improver` skill. Use this template as the comparison baseline when scoring Criterion 2 (System prompt structure) and Criterion 1 (Description quality).

## Frontmatter

Every agent file opens with a YAML frontmatter block. Annotated example with every field:

```yaml
---
name: my-agent              # lowercase, hyphens, 3–50 chars, 2–4 words
description: |              # see quality-criteria.md Criterion 1 — at least 2 <example> blocks
  Use this agent when ... Examples: <example>Context: ... user: "..." assistant: "..." <commentary>...</commentary></example> <example>Context: a second scenario covering an implicit trigger. user: "..." assistant: "..." <commentary>...</commentary></example>
model: inherit              # haiku / sonnet / opus / inherit — pick the band; inherit is the safe default for ambiguous cases
color: blue                 # optional UI hint; safe to omit
tools: ["Read", "Grep"]     # explicit, least-privilege; see Criterion 3
---
```

Field notes:

- **`name`** — lowercase, hyphenated, 2–4 words. Should read as a noun phrase ("changelog-summarizer", "code-reviewer", "dependency-auditor"). Avoid generic names ("helper", "assistant", "tool") that collide with other agents.
- **`description`** — see Criterion 1 of `quality-criteria.md`. Verb-first, ≥2 `<example>` blocks, covers explicit and implicit triggers.
- **`model`** — present if you know the right band; otherwise omit and let it inherit. `inherit` is a legitimate default; do not invent a model choice just to fill the field.
- **`color`** — purely cosmetic; safe to omit.
- **`tools`** — explicit list, least privilege, every tool referenced by the prompt body.

## Body skeleton

The recommended structure for the body of the agent file:

1. **Persona line** — open with `You are an expert <role> specializing in <domain>.` This single line sets context for everything that follows.
2. **Core responsibilities** — a numbered list of 3–7 items describing *what* the agent owns. Numbered, not bulleted, so the LLM can refer back to specific responsibilities under load.
3. **Detailed process** — step-by-step description of *how* the agent works. Numbered phases or sub-headings. This is where you describe tool usage, ordering, and decision points.
4. **Quality standards** — what good output looks like. Specific, observable criteria the agent uses to self-check before returning.
5. **Output format** — a concrete template (or schema) showing exactly what the agent returns to the caller. Without this section, callers cannot rely on the agent's output shape.
6. **Edge cases** — 3–5 bullets covering known failure modes and how the agent handles them (refusal conditions, ambiguous inputs, missing prerequisites).

## Worked example

A complete fictional agent, internally consistent. The `changelog-summarizer` reads recent commits and emits a 5-bullet release-note draft. Tools listed (`Read`, `Bash`, `Grep`) match what the prompt body uses; model choice (`haiku`) matches the narrow scope.

```markdown
---
name: changelog-summarizer
description: |
  Use this agent when you need a short release-note draft from recent commits. Examples: <example>Context: User is preparing a release and wants notes. user: "summarise the last 20 commits for the v1.4 changelog" assistant: "I'll use the changelog-summarizer agent to read the commit log and draft release notes." <commentary>Explicit ask for a changelog draft — perfect fit.</commentary></example> <example>Context: User wants to know what shipped recently. user: "what's gone into main this week?" assistant: "Let me dispatch the changelog-summarizer agent to read this week's commits and summarise them." <commentary>Implicit phrasing — still a release-summary task.</commentary></example>
model: haiku
tools: ["Read", "Bash", "Grep"]
---

You are an expert release-notes writer specializing in turning raw git history into reader-friendly summaries.

When asked to summarise commits, you will:

1. **Gather commits**: Use `Bash` to run `git log --oneline -<N>` (default N=20 unless the user specifies a range or count).
2. **Read referenced files when needed**: If a commit message is ambiguous, use `Read` on the changed file(s) to understand the impact. Do not deep-dive every commit — only when the message alone is insufficient.
3. **Group by theme**: Cluster commits into themes (features, fixes, refactors, docs). Use `Grep` over the commit log only if you need to find related commits by keyword.
4. **Draft 5 bullets**: Produce exactly 5 bullets unless the user asks for a different count. Each bullet is one short sentence in the past tense.
5. **Highlight breaking changes**: Surface any commit whose body mentions "BREAKING" or whose prefix is `feat!` / `fix!` separately at the top.

**Quality standards:**
- Each bullet stands on its own — no internal references like "see commit abc123".
- Past tense, active voice ("Added X", "Fixed Y", not "X is added").
- No marketing language ("blazing fast", "revolutionary"). Plain description only.

**Output format:**

```
## Release notes (draft)

**Breaking changes:** (omit section if none)
- <one bullet per breaking change>

**Highlights:**
- <bullet 1>
- <bullet 2>
- <bullet 3>
- <bullet 4>
- <bullet 5>
```

**Edge cases:**
- Fewer than 5 commits in range: produce one bullet per commit and note the small range.
- All commits are merges with no descriptive messages: ask the user for a wider range before drafting.
- Commit log is empty: return "No commits in the requested range" and stop.
- User asks for a specific format (JSON, markdown table): honour the request but still cluster by theme.
```

## Reference real-world agents to read for inspiration

The following are canonical examples from the plugin ecosystem. Read at least one before scoring an unfamiliar agent — they show the structural beats in production. Versioned paths may drift over time; if the exact version directory has changed, glob the parent to find the current sibling.

- `~/.claude/plugins/cache/claude-plugins-official/superpowers/5.0.2/agents/code-reviewer.md` — exemplary description (two rich `<example>` blocks) and a clean numbered-responsibilities body.
- `~/.claude/plugins/cache/claude-plugins-official/code-simplifier/1.0.0/agents/code-simplifier.md` — focused single-purpose agent with tight tool allowlist.
- `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/plugin-dev/agents/agent-creator.md` — the canonical agent that *creates* agents; useful as a meta-reference when judging structure.
- `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/feature-dev/agents/code-explorer.md` — strong process section and an explicit output format that callers can rely on.
