# Agent Quality Rubric

This file is loaded on demand when the `agent-improver` skill is scoring agents. It expands the six-criterion table referenced from `SKILL.md` Phase 2 and provides the per-criterion signals, examples, and tie-breaking guidance the skill uses when producing its Quality Report. The total score is **100 points**, split as **25 / 25 / 15 / 10 / 15 / 10** across the six criteria below — those weights match the Report Format table in `SKILL.md` and must not be changed.

## How to score

Score every editable agent (project + user scopes) against the six criteria below. For each criterion use this exact structure:

- **What this measures** — the underlying property being assessed.
- **Signals (full points)** — what an excellent agent looks like.
- **Signals (partial)** — what a mediocre agent looks like; partial bands list each weak signal and the per-criterion partial table gives a specific point value per count, so two evaluators converge on the same score.
- **Signals (zero)** — when no credit is earned.
- **Good example** — a real or plausible snippet that would score full marks.
- **Bad example** — a snippet that would score zero.

Sum the six criterion scores to a total `NN/100` and assign a letter grade using the bands at the bottom of this file.

## Criterion 1: Description quality (25 pts)

**What this measures.** Whether the YAML `description` field reliably triggers the agent when the user phrases the request naturally, *and* when they phrase it indirectly.

**Signals (full points, 25).**
- Description starts with a verb that names the action (e.g. "Review", "Audit", "Refactor", "Generate").
- Contains **at least 2 `<example>` blocks**, each with `Context:`, `user:`, `assistant:`, and a `<commentary>` line.
- Covers both **explicit triggers** ("review my code") and **implicit / pushy triggers** ("is this safe to merge?", "does this look right?").
- Is **specific, not generic** — "review React components for accessibility" beats "review code".

**Signals (partial, 10–20).**
- Exactly one `<example>` block.
- Action verb is clear but the scope is generic ("review code" with no domain).
- Covers only the explicit phrasing; implicit triggers absent.

**Partial scoring table** (count how many of the three partial signals are present):

| Partial signals present | Score |
|---|---:|
| 0 (all full-points signals satisfied) | 25 |
| 1 | 20 |
| 2 | 15 |
| 3 | 10 |

**Signals (zero).**
- No `<example>` blocks at all.
- Single-sentence vague description such as `description: helps with code`.
- Starts with a noun or adjective instead of a verb.

**Good example** (from `~/.claude/plugins/cache/claude-plugins-official/superpowers/5.0.2/agents/code-reviewer.md`):

```yaml
description: |
  Use this agent when a major project step has been completed and needs to be reviewed against the original plan and coding standards. Examples: <example>Context: The user is creating a code-review agent that should be called after a logical chunk of code is written. user: "I've finished implementing the user authentication system as outlined in step 3 of our plan" assistant: "Great work! Now let me use the code-reviewer agent to review the implementation against our plan and coding standards" <commentary>Since a major project step has been completed, use the code-reviewer agent to validate the work against the plan and identify any issues.</commentary></example> <example>Context: User has completed a significant feature implementation. user: "The API endpoints for the task management system are now complete - that covers step 2 from our architecture document" assistant: "Excellent! Let me have the code-reviewer agent examine this implementation to ensure it aligns with our plan and follows best practices" <commentary>A numbered step from the planning document has been completed, so the code-reviewer agent should review the work.</commentary></example>
```

**Bad example.**

```yaml
description: helps with code
```

No verb-first action, no examples, no domain — under-triggers in almost every realistic phrasing.

## Criterion 2: System prompt structure (25 pts)

**What this measures.** Whether the body of the agent file is organised so the LLM can follow it under load, and whether the structure communicates persona, responsibilities, process, and output expectations.

**Signals (full points, 25).**
- Opens with a `You are an expert ... specializing in ...` persona line.
- Numbered list of **core responsibilities** (3–7 items).
- Explicit **process steps** (numbered or sub-headed).
- An **output format** section showing what the agent returns to the caller.
- An **edge cases** or **refusal conditions** section covering known failure modes.

**Signals (partial, 10–20).**
- Has persona + responsibilities but is missing process steps OR output format.
- Has structure but uses prose paragraphs instead of headings.

**Partial scoring** (count missing full-points signals — persona / responsibilities / process / output format / edge cases): 1 missing → 20, 2 → 15, 3 → 10. 4+ missing falls into the zero band below.

**Signals (zero).**
- Single-paragraph blob of prose with no headings or numbered list.
- Pure bullet-point dump with no organising structure.

**Good example.** See `references/agent-template.md` for the canonical structure (persona → numbered responsibilities → process → quality standards → output format → edge cases). Plugin agents such as `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/feature-dev/agents/code-architect.md` demonstrate the same structural beats and are useful to read alongside the template.

**Bad example.** A single-paragraph blob:

> You help review code. Read the diff and tell the user what you think. Be helpful.

No persona framing, no responsibilities list, no process, no output spec, no edge cases.

**Note for skills (not agents).** The `You are X` persona convention applies to **agents**, not to **skills**. When this skill is asked to score *itself* or another skill (e.g. via `agent-improver` cross-checking a sibling), Criterion 2 is **partially N/A** for the persona signal — skills are imperative procedures and do not need a "You are" opener. In that case, score Criterion 2 on **structural clarity alone**: are there clear sections (overview, workflow / phases, examples), and is the structure scannable? Award full marks if the skill's structure is clear, even without a persona line. Do not deduct for the missing persona convention.

## Criterion 3: Tool allowlist (15 pts)

**What this measures.** Whether the `tools:` frontmatter follows least-privilege and matches what the prompt actually does.

**Signals (full points, 15).**
- Explicit list of tools (no wildcard).
- **Least privilege**: review/audit agents lack `Write` and `Edit`; pure-analysis agents lack `Bash` unless the prompt uses it.
- Every tool listed is referenced — directly or by clear implication — in the prompt body.

**Signals (partial, 6–12).**
- Explicit list, but it contains 1–2 tools the prompt never uses (over-granted).
- Explicit list, but missing one tool the prompt clearly needs (under-granted).

**Partial scoring**: 1 over- or under-granted tool → 12; 2 → 9; 3 → 6. 4+ falls into zero.

**Signals (zero).**
- `tools:` field is missing entirely (defaults to `*`) AND the agent's role is narrow enough that a tighter list is clearly required.
- `tools: *` (wildcard) for a narrow role such as "review" or "analyze".

**Good example.**

```yaml
tools: ["Read", "Grep"]
```

…on a read-only audit agent whose prompt only inspects files.

**Bad example.**

```yaml
tools: *
```

…on the same read-only audit agent. A wildcard grants `Write`, `Bash`, and `Edit` even though the prompt never exercises them — that is the textbook over-grant.

## Criterion 4: Model choice fit (10 pts)

**What this measures.** Whether the `model:` field matches the agent's reasoning complexity.

**Signals (full points, 10).**
- `model` is present.
- Matches complexity: `haiku` for narrow lookups and mechanical transforms, `sonnet` for everyday agents (most cases), `opus` for heavy reasoning or large-context synthesis, `inherit` when the right band is genuinely undecided or the agent should follow whatever the calling session is using.

**Signals (partial, 4–7).**
- Present but mismatched by **one band** in either direction. Examples: `opus` for a trivial one-shot transform, or `haiku` for a multi-stage planning agent.

**Partial scoring**: 1-band mismatch → 7; 2-band mismatch → 4. 3-band mismatch falls into zero.

**Signals (zero / note-only).**
- Field missing entirely. To resolve, **first classify the agent's complexity from the body** using Criterion 2 signals (responsibilities count, process depth, presence of multi-stage reasoning). Then:
  - If the body clearly maps to one band (e.g. a one-shot file-name normalizer obviously wants `haiku`), score this criterion *as if that model were declared and mismatched* — so a clear-haiku-fit agent with missing field scores 4 (a 2-band gap from a hypothetical `opus`). The rationale: the rule must reward an agent that volunteers a correct `model:` more than one that leaves it implicit.
  - If the body is **genuinely ambiguous between two adjacent bands** (e.g. could be `sonnet` or `haiku`), treat the missing field as a *note* ("consider adding `model: inherit` explicitly") and award full points. `inherit` is the right default for genuinely ambiguous cases.

**Good example.**

```yaml
model: haiku
```

…on a one-shot file-name normalizer.

**Bad example.**

```yaml
model: opus
```

…on the same one-shot normalizer — over-provisions a heavyweight model for a mechanical task.

## Criterion 5: Distinctiveness (15 pts)

**What this measures.** Whether the agent occupies a unique trigger area in the catalogue — cross-checked against **project**, **user**, and **plugin** scopes from the Phase 1 catalogue.

**Signals (full points, 15).**
- No other agent shares **both** the primary verb (`review`, `audit`, `refactor`, etc.) **and** the primary noun-domain (`React components`, `pull request`, `migration`, etc.) in its triggers. Two agents both triggering on "review" + "pull request" overlap; one triggering on "review code" and another on "audit dependencies" do not.
- If the topic is generic (e.g. "code review"), the agent adds clear project-specific value (knowledge of `.ai/decisions.md`, local conventions, internal tooling) that a generic plugin agent cannot.

**Signals (partial, 6–12).**
- Overlaps with a plugin agent on the verb+noun-domain test, but the project/user agent adds meaningful project-specific knowledge.
- Two project/user agents overlap but address different sub-domains (recommend description sharpening rather than removal).

**Partial scoring**: overlaps with 1 sibling but adds project-specific value → 12; overlaps with 1 sibling and value-add is borderline → 9; overlaps with 2 siblings → 6.

**Signals (zero).**
- Near-duplicate of an existing plugin agent with no added value (verbatim or near-verbatim description; identical trigger phrasings; no project context).

**Good example.** A local `code-reviewer` agent that also checks compliance with the project's `.ai/decisions.md` and references the project's lint config — i.e. it specialises the generic concept with local knowledge.

**Bad example.** A local `code-reviewer` whose description and body are copy-pasted from `superpowers:code-reviewer` with no project-specific additions. It collides on triggers and adds nothing — Phase 5's duplication flag should surface it.

## Criterion 6: Currency (10 pts)

**What this measures.** Whether the agent's prompt references things that still exist today — paths, tools, commands, sibling agents.

**Signals (full points, 10).**
- All paths referenced in the prompt still exist in the repository.
- All Claude Code tools referenced still exist (no references to retired tools).
- All commands cited work as written today.
- Sibling agent / skill names referenced in the body still resolve.

**Signals (partial, 4–7).**
- 1–2 stale references (e.g. one path that was renamed, one tool that was deprecated but has an obvious successor).

**Partial scoring**: 1 stale reference → 7; 2 stale references → 4. 3+ falls into zero.

**Signals (zero).**
- Multiple broken references (3+).
- The agent's central workflow depends on a path or command that no longer exists.

**Good example.** Prompt cites `scripts/lint.sh` and that file exists in the repo today.

**Bad example.** Prompt cites `scripts/lint-v1-deprecated.sh` which was deleted six months ago and the agent's process step says "run `./scripts/lint-v1-deprecated.sh` first" — the agent's workflow is broken.

## Grade bands

- **A (90–100)** — production-ready; nothing to fix.
- **B (75–89)** — solid; one or two small improvements would polish it.
- **C (60–74)** — functional but missing structure or precision; recommend updates.
- **D (40–59)** — weak; under-triggers or over-grants tools; recommend a focused rewrite.
- **F (<40)** — broken or unsafe; recommend a full rewrite, or consolidation with a sibling agent.

These bands match the bands stated in `SKILL.md` Phase 2 and must not drift.

## How to compute "Recommended changes"

When generating the **Recommended changes** block under each agent in the Quality Report, follow these rules:

1. **Focus on the lowest-scoring criterion first.** One fix per criterion per pass — do not dump every possible improvement at once. The user is more likely to approve a short, targeted list.
2. **Never recommend changes that would lose information already in the prompt.** If a prompt has a quirky-but-functional section, propose *adding* structure around it rather than rewriting it.
3. **Always show diffs for proposed edits** (frontmatter or body), so the user can approve or reject precisely. The diff is what gets applied via `Edit` in Phase 5.
4. **If two criteria tie for lowest, prioritise safety over ergonomics.** Concretely: **Tool allowlist (Criterion 3)** wins over **Description quality (Criterion 1)** when both are equally low. Over-granted tools are a real safety surface; an under-triggered description is an ergonomics annoyance. Fix the safety issue first.
5. Phrase each recommendation as an imperative ("Add an `<example>` block covering the implicit phrasing", "Remove `Write` from `tools` since the prompt never modifies files"), not as a description ("the description could be better").
