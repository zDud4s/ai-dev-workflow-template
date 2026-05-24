"""Regression tests for the proposals-A11Y batch of fixes.

Covers four targeted patches:
  1. skills.js — skill cards expose tabindex="0" and role="button" so they
     are focusable and announce as buttons to assistive tech.
  2. agents.js — agent cards expose the same a11y attributes.
  3. skills.js / agents.js — a delegated keydown listener on the grid
     containers activates Enter/Space on focused cards so keyboard users
     reach the same opener path as mouse users.
  4. skills.js openProposalModal — the dead-branch ternary
     `draftStuck ? "Create skill" : "Create skill"` is collapsed to a
     single literal, because both branches produced the same string.

Source-text assertions only (no browser). The previous batches'
integration tests already exercise the modal/opener flow end-to-end.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def test_skill_card_has_tabindex_role():
    """Fix 1: rendered .skill-card markup must carry both tabindex="0" and
    role="button" so focus and screen-reader semantics are correct."""
    src = _src("skills.js")
    # Locate the skill-card template line in renderSkillsGrid (the one with
    # data-source + data-name — the catalog card, not the skeleton or the
    # proposal card).
    pattern = re.compile(
        r'class="card skill-card"[^`]*tabindex="0"[^`]*role="button"[^`]*data-source=',
        re.DOTALL,
    )
    assert pattern.search(src), (
        "skill-card template must include tabindex=\"0\" and role=\"button\""
    )


def test_agent_card_has_tabindex_role():
    """Fix 2: rendered .agent-card markup must carry both tabindex="0" and
    role="button". The class string is "card skill-card agent-card"."""
    src = _src("agents.js")
    pattern = re.compile(
        r'class="card skill-card agent-card"[^`]*tabindex="0"[^`]*role="button"[^`]*data-source=',
        re.DOTALL,
    )
    assert pattern.search(src), (
        "agent-card template must include tabindex=\"0\" and role=\"button\""
    )


def test_skills_grid_handles_keydown():
    """Fix 1: skills.js must wire a keydown listener on the grid (delegated
    pattern) and guard it with _skillsGridKeydownWired to avoid double-wire
    on re-renders."""
    src = _src("skills.js")
    assert "_skillsGridKeydownWired" in src, (
        "skills.js must declare _skillsGridKeydownWired guard flag"
    )
    assert 'addEventListener("keydown"' in src, (
        "skills.js must register a keydown listener for card activation"
    )
    # The listener must filter to Enter/Space — anything else is a wider
    # surface than intended and may swallow shortcuts.
    assert 'e.key !== "Enter"' in src and 'e.key !== " "' in src, (
        "skills.js keydown handler must only act on Enter or Space"
    )


def test_agents_grid_handles_keydown():
    """Fix 2: same pattern in agents.js — _agentsGridKeydownWired guard
    plus a keydown listener limited to Enter/Space."""
    src = _src("agents.js")
    assert "_agentsGridKeydownWired" in src, (
        "agents.js must declare _agentsGridKeydownWired guard flag"
    )
    assert 'addEventListener("keydown"' in src, (
        "agents.js must register a keydown listener for card activation"
    )
    assert 'e.key !== "Enter"' in src and 'e.key !== " "' in src, (
        "agents.js keydown handler must only act on Enter or Space"
    )


def test_skills_draft_button_dead_branch_removed():
    """Fix 3: the `draftStuck ? "Create skill" : "Create skill"` dead-branch
    ternary must be gone — both arms were the same literal."""
    src = _src("skills.js")
    assert 'draftStuck ? "Create skill" : "Create skill"' not in src, (
        "dead-branch ternary on draft button label must be collapsed"
    )
    # Sanity: the collapsed form is still present.
    assert 'isDraft ? "Create skill" : "Accept"' in src, (
        "collapsed draft-button label assignment must remain"
    )
