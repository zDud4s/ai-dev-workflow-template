---
name: maintenance
description: Maintain the mutable project layer after repo changes or completed tasks. Refresh project metadata, commands, boundaries, memory, and local guidance without changing the workflow core.
---

You are the maintenance skill.

Purpose:
Keep the project-specific workflow layer accurate over time without degrading the core workflow.

## Prerequisite

Check `.ai/project.yaml`. If `project_name` is `unknown` and `stack` is empty, STOP and tell the user: "Run the bootstrap skill first. The project metadata is empty."

## Core principle

The workflow core is immutable by default. Only maintain the mutable project layer unless the user explicitly asks for a core workflow change.

Immutable core:
- root AGENTS.md workflow roles
- escalation policy
- core packet schemas
- planner / reviewer / rescue / bootstrap skill behavior
- safety boundaries

Mutable project layer:
- `.ai/project.yaml`
- `.ai/memory.md`
- `.ai/decisions.md`
- local AGENTS.md files inside project subdirectories

## When to use

- After bootstrap
- After completing a task that revealed new repo facts
- After build config, package manager, or CI changes
- After discovering new fragile modules or risky areas
- After repeated failures caused by stale project metadata
- After any review that lists memory updates to apply
- When asked to "refresh", "maintain", or "update the workflow"
- When `.ai/memory.md` exceeds 150 lines or contains contradictions (triggers a consolidation pass — see below)
- When `.ai/memory.md` or `.ai/project.yaml` carry verbose phrasing (entries > 25 words, yaml leaves with prose) or exceed their token budget — triggers a density pass (see below)

## Responsibilities

1. Re-scan the repository structure when relevant.
2. Refresh detected commands if scripts, package managers, or entrypoints changed.
3. Update ownership, important directories, risky areas, and do-not-touch zones in `.ai/project.yaml`.
4. Append operational discoveries to `.ai/memory.md`.
5. Record stable architectural decisions in `.ai/decisions.md` only when evidence is strong.
6. Tighten local subdirectory AGENTS.md files if the structure clearly changed.
7. Remove stale assumptions when they are disproven.
8. Keep the project layer concise, factual, and operational — phases pay token cost for every line they load.
9. Run a density pass when phrasing is verbose or files exceed their token budget (see below). Phases read these files; humans don't — optimize for prompt cost.

## Never do without explicit user approval

- Rewrite root workflow roles
- Change the core escalation policy
- Loosen safety boundaries
- Rewrite planner/reviewer/rescue logic
- Silently broaden project scope
- Invent commands and present them as confirmed facts

## File-specific rules

`.ai/project.yaml`: keep values structured and compact. Update only with repo evidence. Keep assumptions explicit. **Token budget ~800 tokens** (≈3200 chars). Leaf values stay short (≤30 chars where possible); no prose, no decorative descriptions, no empty/null placeholder fields — drop them.

`.ai/memory.md`: append short operational facts using the dated format `- YYYY-MM-DD [topic] fact`. Remove stale items when disproven. Do not turn it into a changelog. Run a consolidation pass when triggers fire (see below). **Token budget ~2000 tokens** (≈8000 chars, roughly 150 dense lines). Each entry ≤15 words; one fact per line; paths/commands in backticks.

`.ai/decisions.md`: record only stable decisions. Each must include why it exists. No temporary debugging choices.

Local subdirectory AGENTS.md: keep narrower than root AGENTS.md. Only add local constraints that truly help execution.

## Consolidation pass (memory)

Append-only memory accumulates duplicates, contradictions, and stale facts. Run a consolidation pass on `.ai/memory.md` when ANY trigger fires:

1. **Size** — post-append projected line count > current threshold (read `memory_tuning.consolidation_threshold_lines` from `.ai/project.yaml`, default 150). Consolidate BEFORE appending new entries.
2. **Contradiction** — a new memory update contradicts an existing entry (same topic, incompatible fact). Reconcile both at once.
3. **Explicit** — the user or orchestrator requests "consolidate memory" / "compact memory".

### Procedure

1. Read `.ai/memory.md`. Parse entries; treat anything not matching `- YYYY-MM-DD [topic] fact` as "undated".
2. **Deduplicate.** If two entries express the same fact, keep the newer date and the clearer wording. Merge supporting detail into a single line if possible.
3. **Merge related.** Group entries by `[topic]`. Within a topic, collapse closely-related lines that can be expressed as one without losing operational value.
4. **Detect contradictions.** Two entries with the same topic but incompatible content. For each conflict:
   - If one is dated and the other undated → keep the dated one, drop the undated.
   - If both dated → keep the newer; surface the conflict in the maintenance output.
   - If both undated → surface as a conflict needing human resolution; do NOT silently pick.
5. **Archive obsolete.** Entries whose subject (file path, command, module, dependency) no longer exists in the repo, OR which are point-in-time snapshots whose claim is no longer time-relevant, are MOVED to .ai/memory-archive.md (append with " (archived: <today> <reason>)" suffix) — NOT deleted. Note each move in the output.
6. **Re-sort.** Group by topic alphabetically, then by date within each topic.
7. **Cap.** If the result still exceeds 150 lines, prefer trimming the oldest entries within each topic, never the conflict markers.

### Output

After consolidation, report:
- `Entries before / after` (counts)
- `Lines before / after` (counts — used for the compaction ratio)
- `Deduplicated` (count + a few examples)
- `Merged` (count)
- `Dropped as obsolete` (each line + reason)
- `Archived: <count> → memory-archive.md`
- `Conflicts surfaced` (each pair, marked for user decision)
- `Final size` (lines)
- `Compaction ratio` (lines_before / lines_after, to 2 decimals)
- `Threshold update` (old → new, see "Adaptive threshold" below)

The archive is human-inspection-only; no phase loads it by default.

Do NOT silently rewrite the user's facts. Every removal or merge must appear in the output above.

### Adaptive threshold

After every consolidation pass, recompute `memory_tuning.consolidation_threshold_lines` in `.ai/project.yaml` based on the smoothed compaction ratio. This lets the trigger self-tune to how noisy the project's memory actually is.

**Inputs (read from `memory_tuning` in `.ai/project.yaml`):**
- `consolidation_threshold_lines` (current threshold)
- `floor` (default 50), `ceiling` (default 300) — hard bounds
- `last_ratios` (list, newest first, max 3 entries)

**Step 1 — append this run's ratio.** Push the new `lines_before / lines_after` to the front of `last_ratios`. Truncate to 3 entries.

**Step 2 — compute smoothed ratio.** Arithmetic mean of `last_ratios`. If only one ratio is present, use it directly. Treat division-by-zero (lines_after == 0) as ratio = 999 (extreme bloat).

**Step 3 — adjust threshold.** Apply the band rule:

| Smoothed ratio | Interpretation | Threshold change |
|---|---|---|
| ≥ 1.5 | Memory was bloated; consolidation cut ≥33% | × 0.85 (tighten) |
| 1.2 – 1.49 | Some redundancy | × 0.95 (gentle tighten) |
| 1.05 – 1.19 | Roughly right | no change |
| < 1.05 | Threshold may be too tight (barely cut anything) | × 1.15 (loosen) |

**Step 4 — clamp.** Round to nearest integer. Clamp to `[floor, ceiling]`. If clamped, note this in the output ("threshold clamped to ceiling").

**Step 5 — write back.** Update `memory_tuning.consolidation_threshold_lines`, `memory_tuning.last_ratios`, and `memory_tuning.last_consolidated_at` (today's date) in `.ai/project.yaml`.

The threshold change appears in the consolidation output as `Threshold update: <old> → <new> (ratio <smoothed>)`. If the change exceeded ±20% in a single pass, prepend a one-line note explaining the swing — sudden swings are usually first-time tuning or a one-off cleanup.

## Density pass (token optimization)

Consolidation handles **quantity** (dedupe, archive, contradictions). Density handles **quality** (terse phrasing per entry). Both files are read by phases as prompt context — every redundant word is a recurring token cost.

Trigger ANY of:
1. **Budget** — `.ai/memory.md` projected size > token budget (~2000 tokens) OR `.ai/project.yaml` > ~800 tokens. Estimate tokens as `chars / 4`.
2. **Phrasing** — any memory entry > 25 words; any yaml leaf with prose narrative (>30 chars of non-name text); any field set to `null`, `~`, `""`, or `TODO`.
3. **Explicit** — user requests "compact", "optimize tokens", "tighten memory", or similar.

### Rewrite rules — memory.md

- Keep format `- YYYY-MM-DD [topic] fact`. Preserve original date and topic; rewrite only the fact.
- Drop filler: "we discovered that", "it turns out", "after running", "when we tried", "the user reported".
- Drop narrative tense: "when X ran, Y happened" → "X causes Y" or "X → Y".
- Drop redundant subjects: "the database is configured to use pooling" → "db: pooled".
- Paths, commands, filenames in backticks, not described in prose.
- One fact per line; split compound entries into separate dated lines.
- Target ≤15 words per entry. Hard cap 25 words.

### Rewrite rules — project.yaml

- Leaf values ≤30 chars where possible. Multi-word identifiers OK; full sentences NOT.
- Drop empty/null/placeholder leaves entirely (`null`, `~`, `""`, `TODO`, `unknown`) — unless `unknown` is meaningful state (e.g., `project_name: unknown` is a bootstrap signal).
- Lists: one short item per line, no decorative descriptions.
- No commentary inside values. Comments via `# ...` only when load-bearing for phases.
- Collapse nested objects with a single child up one level when it doesn't lose meaning.

### Procedure

1. Read both files. Compute current `chars / 4` as token estimate.
2. Scan for verbose entries (memory) and prose/placeholder leaves (yaml). Score: token cost vs operational value.
3. Rewrite high-cost low-density entries in place. Preserve original date and topic prefix in memory entries.
4. Drop fields/leaves that carry no operational value for any phase. List each in the output.
5. Re-estimate tokens. If still over budget, surface the largest remaining entries for human triage — **never silently truncate**.

### Output

After a density pass, report:
- `memory.md tokens: <before> → <after>` (with delta and % saved)
- `project.yaml tokens: <before> → <after>`
- `Entries rewritten: <count>` — include 2–3 before/after examples
- `Fields dropped: <list>` (from project.yaml)
- `Still over budget` — entries flagged for human decision, if any
- `Untouched` — entries already dense (no rewrite needed)

Do NOT silently rewrite the user's facts. Every rewrite/drop must appear in the output above. If a rewrite changes meaning, surface the change with the original text and ask for confirmation.

### Interaction with consolidation

When both triggers fire on the same run:
1. Density first (cheaper per-entry rewrite reduces what consolidation has to merge).
2. Then consolidation (dedupe collapses now-similar entries).
3. Report both passes; the consolidation ratio is computed on post-density line counts.

## Stop conditions

- Stop if evidence is too weak
- Stop if requested change would alter the immutable core
- Stop if maintenance would require product implementation
- Stop if multiple conflicting interpretations exist with no clear winner

If you stop, output: what is unclear, what evidence is missing, the smallest safe next step.

## Token budget

Maintenance output ≤80 lines.

## Output format

- Scope checked
- Files updated
- Confirmed changes
- Assumptions added / removed
- Unknowns remaining
- Core-change warning (if any)
