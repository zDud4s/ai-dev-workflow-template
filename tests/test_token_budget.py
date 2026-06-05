"""Size budgets on workflow-core files.

These guard the gains from the initial density pass and prevent future bloat from creeping in unnoticed.
If a file exceeds its budget, either justify and raise the cap here, or trim.
Caps are intentionally loose (10-15% headroom over current size) to allow
small additions without churn but block significant re-bloating.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from conftest import REPO_ROOT

# (path-relative-to-repo, max_lines, max_bytes)
BUDGETS = [
    # orchestrate: original 130L/11000B. Bytes bumped for metrics logging section
    # (PR 2) which adds the JSONL schema (compact single-line) consumed by the
    # PR 3 adaptive scorer. Schema cannot be split without losing parser clarity.
    # and codex-dispatched token-capture clarification.
    (".claude/skills/orchestrate/SKILL.md", 132, 12400),
    (".claude/skills/codex/SKILL.md",        60,  5000),
    # gemini SKILL.md never shipped — placeholder budget removed so the
    # test stops failing on a non-existent file. Restore the row when the
    # dispatch-to-Gemini path actually lands.
    # planner: original 85L/5200B. Bumped for auto-select output block (PR 1 chunk 2,
    # +14L/+1.1KB of format rules consumed by the orchestrator regex parser) and
    # adaptive scoring section (PR 3, +1 paragraph/+1KB with the scoring formula,
    # guard rail, cold-start fallback). Sections are at maximum density; further
    # trimming would risk parser/scorer drift.
    (".claude/skills/planner/SKILL.md",     100,  7900),
    (".claude/skills/reviewer/SKILL.md",     70,  3700),
    # maintenance: Round 2 expanded the "Immutable core" enumeration to
    # cover all skills + future skills explicitly (closes D.P1-3). +1L
    # over the prior 175-line ceiling — bump by 5L + 600B for headroom
    # against the next density pass. Raised again for pinned-protection
    # guidance guarding cross-cutting governance facts during size caps.
    (".claude/skills/maintenance/SKILL.md", 185, 9600),
    # plan.md: bumped from 35L/1500B to 45L/2400B in Round 2 to declare the
    # `Problem summary`, `Memory tags`, `## Execution packet(s)`, and
    # `## Selected models` fields the planner emits but the template was
    # missing (closes D.P0-2). Comments kept terse so the schema stays loadable.
    (".ai/packets/plan.md",                  45,  2400),
    (".ai/packets/execute.md",               50,  2100),
    (".ai/packets/review.md",                35,  1500),
    (".ai/packets/rescue.md",                25,   900),
    (".ai/ledgers/todos.jsonl",            1000, 100000),
    # memory.md: local per-checkout file (skip-worktree). The committed
    # template is empty; each checkout grows it as tasks land. Pin a
    # generous-but-bounded ceiling so a real bloat (>120L / >12KB) still
    # trips but routine working states don't fail the suite. Consolidation
    # via the `maintenance` skill is the canonical fix when it crosses 150L.
    (".ai/memory.md",                       120, 12000),
]


@pytest.mark.parametrize(
    "rel,max_lines,max_bytes",
    BUDGETS,
    ids=[b[0] for b in BUDGETS],
)
def test_file_within_budget(rel: str, max_lines: int, max_bytes: int):
    p = REPO_ROOT / rel
    if not p.is_file():
        pytest.skip("file does not exist yet")
    data = p.read_bytes()
    text = data.decode("utf-8")
    lines = len(text.splitlines())
    size = len(data)
    assert lines <= max_lines, f"{rel}: {lines} lines > budget {max_lines}"
    assert size <= max_bytes, f"{rel}: {size} bytes > budget {max_bytes}"


def test_packet_comment_density():
    """Packet schemas should be mostly schema, not prose.

    Ambient guidance belongs in .ai/packets/README.md (see design D1).
    Heuristic: fewer than 30% of non-blank lines should be HTML-comment lines.
    """
    for name in ("plan.md", "execute.md", "review.md", "rescue.md"):
        p = REPO_ROOT / ".ai" / "packets" / name
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            continue
        comment_lines = sum(1 for l in lines if l.strip().startswith("<!--"))
        ratio = comment_lines / len(lines)
        assert ratio < 0.30, (
            f".ai/packets/{name}: {ratio:.0%} of non-blank lines are HTML comments "
            f"(budget < 30%). Move prose to .ai/packets/README.md."
        )


def test_memory_archive_split_exists():
    """memory.md must have a sibling archive once Chunk 2 ships."""
    assert (REPO_ROOT / ".ai" / "memory-archive.md").is_file(), (
        ".ai/memory-archive.md missing; see design D5"
    )


def test_packets_readme_exists():
    """.ai/packets/README.md holds the prose stripped from schemas (design D1)."""
    assert (REPO_ROOT / ".ai" / "packets" / "README.md").is_file(), (
        ".ai/packets/README.md missing; see design D1"
    )
