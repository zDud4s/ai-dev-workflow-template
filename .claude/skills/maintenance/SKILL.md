---
name: maintenance
description: Maintain the mutable project layer after repo changes or completed tasks. Refresh project metadata, commands, boundaries, memory, and local guidance without changing the workflow core.
---

Keep the project-specific workflow layer accurate without degrading the workflow core.

## Prerequisite

If `.ai/project.yaml` has `project_name: unknown` and `stack` empty, STOP: "Run the bootstrap skill first. The project metadata is empty."

## Core principle

The workflow core is immutable by default. Only maintain the mutable project layer unless the user explicitly asks for a core change.

Immutable core: root AGENTS.md, escalation policy, packet schemas, planner/reviewer/rescue/bootstrap behavior, safety boundaries.

Mutable layer: `.ai/project.yaml`, `.ai/memory.md`, `.ai/decisions.md`, local AGENTS.md inside subdirectories.

## When to use

- After bootstrap, or a task that revealed new repo facts
- After build/package/CI changes, or new fragile modules
- After repeated failures from stale metadata, or a review listing memory updates
- On "refresh" / "maintain" / "update the workflow"
- When `.ai/memory.md` > 150 lines or has contradictions → consolidation pass
- When entries > 25 words, yaml leaves carry prose, or files exceed token budget → density pass

## Responsibilities

1. Re-scan repo structure when relevant.
2. Refresh detected commands when scripts/PM/entrypoints changed.
3. Update ownership, important dirs, risky areas, do-not-touch zones in `.ai/project.yaml`.
4. Append operational discoveries to `.ai/memory.md`.
5. Record stable architectural decisions in `.ai/decisions.md` only with strong evidence.
6. Tighten local subdirectory AGENTS.md when structure clearly changed.
7. Remove disproven assumptions.
8. Keep the project layer concise, factual, operational — phases pay token cost per line.
9. Run a density pass when phrasing is verbose or files exceed budget.

## Never do without explicit user approval

- Rewrite root workflow roles
- Change the core escalation policy
- Loosen safety boundaries
- Rewrite planner/reviewer/rescue logic
- Silently broaden project scope
- Invent commands as confirmed facts

## File-specific rules

`.ai/project.yaml`: structured + compact. Repo-evidence only; assumptions explicit. **Budget ~800 tokens (~3200 chars).** Leaves ≤30 chars; no prose, decorative descriptions, or empty/null placeholders.

`.ai/memory.md`: format `- YYYY-MM-DD [topic] fact`. Drop stale when disproven. Not a changelog. Consolidate on trigger. **Budget ~2000 tokens (~8000 chars, ~150 dense lines).** ≤15 words/entry; one fact/line; paths and commands in backticks.

`.ai/decisions.md`: only stable decisions, each with a why. No temporary choices.

Local subdirectory AGENTS.md: narrower than root; only local constraints that truly help execution.

## Consolidation pass (memory)

Append-only memory accumulates duplicates, contradictions, stale facts. Trigger on ANY of:

1. **Size** — projected lines > current threshold (`memory_tuning.consolidation_threshold_lines` in `.ai/project.yaml`, default 150). Consolidate BEFORE appending.
2. **Contradiction** — new update contradicts an existing entry. Reconcile at once.
3. **Explicit** — user/orchestrator requests "consolidate memory" / "compact memory".

### Procedure

1. Parse entries; anything not matching `- YYYY-MM-DD [topic] fact` is "undated".
2. **Deduplicate.** Same fact → keep newer date + clearer wording; merge detail into one line.
3. **Merge related.** Group by `[topic]`; collapse closely-related lines when no operational value is lost.
4. **Contradictions** (same topic, incompatible content):
   - dated vs undated → keep dated, drop undated
   - both dated → keep newer; surface in output
   - both undated → surface for human; don't silently pick
5. **Archive obsolete.** Subject gone from repo, or point-in-time claim now stale → MOVE to `.ai/memory-archive.md` (append ` (archived: <today> <reason>)`), NOT delete. List each.
6. **Re-sort.** By topic alphabetically, then date within topic.
7. **Cap.** If still > 150 lines, trim oldest within each topic — never conflict markers.

### Output

Report: entries before/after, lines before/after, deduplicated (with examples), merged, dropped (line + reason), archived count, conflicts surfaced, final lines, compaction ratio (`before/after`, 2dp), threshold update.

Archive is human-inspection-only; no phase loads it. Do NOT silently rewrite user facts — every removal/merge appears in the output.

### Adaptive threshold

After every pass, recompute `memory_tuning.consolidation_threshold_lines` from the smoothed compaction ratio.

Inputs in `memory_tuning`: `consolidation_threshold_lines`, `floor` (default 50), `ceiling` (default 300), `last_ratios` (newest-first, max 3).

1. **Append ratio.** Push this run's `lines_before/lines_after` to front of `last_ratios`; truncate to 3.
2. **Smooth.** Arithmetic mean. Single ratio → use directly. `lines_after == 0` → ratio = 999.
3. **Adjust:**

| Smoothed ratio | Interpretation | Threshold |
|---|---|---|
| ≥ 1.5 | Bloated; cut ≥33% | × 0.85 |
| 1.2 – 1.49 | Some redundancy | × 0.95 |
| 1.05 – 1.19 | Roughly right | no change |
| < 1.05 | Too tight (barely cut) | × 1.15 |

4. **Clamp.** Round, clamp to `[floor, ceiling]`. Note in output if clamped.
5. **Write back.** Update `consolidation_threshold_lines`, `last_ratios`, `last_consolidated_at` (today).

Report `Threshold update: <old> → <new> (ratio <smoothed>)`. If change > ±20% in one pass, prepend a one-line note (usually first-time tune or one-off cleanup).

## Density pass (token optimization)

Consolidation = **quantity** (dedupe, archive). Density = **quality** (terse phrasing). Both files load as phase context — redundant words are recurring cost.

Trigger ANY of:
1. **Budget** — `.ai/memory.md` > ~2000 tokens OR `.ai/project.yaml` > ~800 tokens (estimate `chars / 4`).
2. **Phrasing** — memory entry > 25 words; yaml leaf with prose narrative (>30 chars non-name) or `null` / `~` / `""` / `TODO`.
3. **Explicit** — user asks "compact" / "optimize tokens" / "tighten memory" / similar.

### Rewrite rules — memory.md

- Keep format `- YYYY-MM-DD [topic] fact`. Preserve date + topic; rewrite only the fact.
- Drop filler: "we discovered that", "it turns out", "after running", "when we tried", "the user reported".
- Drop narrative tense: "when X ran, Y happened" → "X causes Y" or "X → Y".
- Drop redundant subjects: "db configured to use pooling" → "db: pooled".
- Paths/commands/filenames in backticks, not prose.
- One fact per line; split compounds.
- Target ≤15 words; hard cap 25.

### Rewrite rules — project.yaml

- Leaf values ≤30 chars where possible. Multi-word identifiers OK; full sentences NOT.
- Drop empty/null/placeholder leaves (`null`, `~`, `""`, `TODO`, `unknown`) — unless `unknown` is meaningful state (e.g. bootstrap signal).
- Lists: one short item/line, no decorative descriptions.
- No commentary in values. Comments via `# ...` only when load-bearing.
- Collapse single-child nested objects up one level when meaning is preserved.

### Procedure

1. Read both files. Compute `chars / 4` as token estimate.
2. Scan verbose entries (memory) + prose/placeholder leaves (yaml). Score: token cost vs operational value.
3. Rewrite high-cost low-density entries in place. Preserve date + topic in memory entries.
4. Drop fields/leaves with no operational value for any phase. List each.
5. Re-estimate. Still over budget? Surface largest remaining for human triage — **never silently truncate**.

### Output

Report tokens before→after (delta + %) for both files; entries rewritten with 2–3 before/after examples; fields dropped; still-over-budget flagged for human; untouched (already dense). Every rewrite/drop appears. If meaning changes, surface original and ask.

### Interaction with consolidation

When both trigger on the same run:
1. Density first (cheaper per-entry rewrite reduces what consolidation merges).
2. Then consolidation (dedupe collapses now-similar entries).
3. Report both; consolidation ratio is computed on post-density line counts.

## Stop conditions

- Evidence too weak
- Requested change would alter the immutable core
- Maintenance would require product implementation
- Multiple conflicting interpretations with no clear winner

On stop, output: what is unclear, what evidence is missing, smallest safe next step.

## Token budget

Maintenance output ≤80 lines.

## Output format

- Scope checked
- Files updated
- Confirmed changes
- Assumptions added / removed
- Unknowns remaining
- Core-change warning (if any)
