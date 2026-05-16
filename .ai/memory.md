# Project Memory

Operational facts discovered during work — short, factual, useful for the next task.

## Entry format

```
- YYYY-MM-DD [topic] fact
```

- **Date** = when the fact was learned (or last reconfirmed). Required for new entries; lets consolidation detect stale items.
- **Topic** = short tag for grouping (e.g. `[build]`, `[ci]`, `[auth]`, `[tests]`). One per entry.
- **Fact** = one line. Wrap if absolutely needed, but prefer concision.

Older undated entries are tolerated; consolidation will date them as "unknown" and treat them as candidates for review when they conflict with newer facts.

## Use this file for

- commands that actually worked
- environment quirks
- failing test clusters
- fragile files or modules
- recurring blocker patterns
- known false assumptions that were corrected

## Do not store

- long prose
- duplicated architecture docs
- task history that is no longer relevant
- decisions (those go to `.ai/decisions.md`)

## Maintenance

The `maintenance` skill periodically runs a **consolidation pass** that deduplicates, merges, flags conflicts, and drops obsolete entries. Triggered when this file exceeds 150 lines, on explicit request, or when a task surfaces a contradiction with an existing entry. See `.claude/skills/maintenance/SKILL.md`.

## Entries
