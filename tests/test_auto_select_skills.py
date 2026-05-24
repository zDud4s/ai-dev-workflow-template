"""Skill-content tests: planner emits block, orchestrator parses it
(spec section: Acceptance PR 1, planner+orchestrator bullets)."""
from __future__ import annotations

import pytest

from conftest import CLAUDE_SKILLS_DIR, WORKFLOW_DIR

PLANNER = CLAUDE_SKILLS_DIR / "planner" / "SKILL.md"
ORCHESTRATE = CLAUDE_SKILLS_DIR / "orchestrate" / "SKILL.md"
DISPATCH = WORKFLOW_DIR / "dispatch.md"


@pytest.fixture(scope="module")
def planner_text():
    return PLANNER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def orchestrate_text():
    return ORCHESTRATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dispatch_text():
    return DISPATCH.read_text(encoding="utf-8")


# --- Planner -----------------------------------------------------------------


def test_planner_has_auto_select_section(planner_text):
    assert "## Auto-select output block" in planner_text, (
        "planner skill must document the Selected models block format"
    )


def test_planner_documents_strict_format(planner_text):
    """Format rules must be strict enough for a regex parser."""
    section = planner_text.split("## Auto-select output block", 1)[1]
    for marker in (
        "## Selected models",
        "tool=",
        "model=",
        "reason=",
        "reasoning_effort",
        "120",
    ):
        assert marker in section, (
            f"planner auto-select section missing reference to {marker!r}"
        )


def test_planner_gates_on_enabled(planner_text):
    """The block is conditional on auto_select.enabled."""
    section = planner_text.split("## Auto-select output block", 1)[1]
    assert "auto_select.enabled" in section
    assert "false" in section.lower() or "omit" in section.lower()


def test_planner_documents_omit_when_effort_na(planner_text):
    """The planner must document that lines with effort=n/a omit reasoning_effort
    (never emit `reasoning_effort=n/a`). The rule applies to any row regardless
    of tool — claude and codex can both carry an explicit effort."""
    section = planner_text.split("## Auto-select output block", 1)[1]
    assert "omit" in section.lower() and "reasoning_effort" in section, (
        "planner must document the omit-when-n/a rule"
    )
    # The old wording hard-coded claude as omit-only. After alignment the
    # planner emits effort for claude rows too, so that hard-coding must be gone.
    assert "(claude rows)" not in section, (
        "planner must no longer state that effort is omitted because the row is claude; "
        "the rule is `effort == n/a`, not tool-specific"
    )


def test_planner_reasoning_effort_set_includes_max(planner_text):
    """`max` is a claude-only effort level. The planner skill must list it
    in the allowed set so the orchestrator's regex parser accepts it."""
    section = planner_text.split("## Auto-select output block", 1)[1]
    assert "max" in section, (
        "planner allowed effort set must include `max` (claude-only)"
    )


def test_planner_documents_adaptive_scoring(planner_text):
    """Planner must document the adaptive scorer (PR 3)."""
    section = planner_text.split("## Auto-select output block", 1)[1]
    assert "auto_select.adaptive" in section, (
        "planner must reference auto_select.adaptive flag"
    )
    for marker in (".ai/metrics.jsonl", "success_rate", "Guard rail", "Cold-start"):
        assert marker in section, (
            f"adaptive scoring section missing reference to {marker!r}"
        )
    # Scoring formula must be present (key terms).
    assert "0.6" in section and "0.2" in section, (
        "scoring formula coefficients (0.6, 0.2, 0.2) must be present"
    )
    # Guard rail threshold.
    assert "0.7" in section, "guard rail success_rate threshold 0.7 must be present"
    # Cold-start sample minimum.
    assert "≥5" in section or ">=5" in section or "5 samples" in section, (
        "cold-start minimum sample count (5) must be present"
    )


# --- Orchestrator ------------------------------------------------------------


def test_orchestrate_has_handoff_subsection(orchestrate_text):
    assert "Auto-select handoff" in orchestrate_text, (
        "orchestrate skill must document the Selected-models handoff"
    )


def test_orchestrate_phase2_uses_auto_overrides(orchestrate_text):
    """Phase 2 must consult auto_overrides before models.yaml."""
    phase2 = orchestrate_text.split("## Phase 2", 1)[1].split("## Phase 3", 1)[0]
    assert "auto_overrides" in phase2 and "execute" in phase2, (
        'Phase 2 must reference auto_overrides["execute"]'
    )


def test_orchestrate_phase3_uses_auto_overrides(orchestrate_text):
    """Phase 3 must consult auto_overrides before models.yaml."""
    phase3 = orchestrate_text.split("## Phase 3", 1)[1].split("## Phase 4", 1)[0]
    assert "auto_overrides" in phase3 and "review" in phase3, (
        'Phase 3 must reference auto_overrides["review"]'
    )


def test_orchestrate_wrap_up_log_has_source_column(orchestrate_text):
    """Wrap-up log example must include source=auto|config."""
    phase4 = orchestrate_text.split("## Phase 4", 1)[1]
    assert "source=auto" in phase4 and "source=config" in phase4, (
        "Phase 4 wrap-up log example must show both source values"
    )


def test_orchestrate_documents_tool_availability_check(orchestrate_text):
    """Auto-selected tool unavailable -> STOP, no silent fallback."""
    assert "auto-selected tool unavailable" in orchestrate_text, (
        "orchestrate skill must document the tool-availability STOP"
    )


# --- Dispatch error table ----------------------------------------------------


def test_dispatch_error_table_has_new_rows(dispatch_text):
    """Three new error rows must appear in the dispatch error table."""
    for marker in (
        "Selected models block missing",
        "Selected models block malformed",
        "auto-selected tool unavailable",
    ):
        assert marker in dispatch_text, (
            f"dispatch error table missing row referencing {marker!r}"
        )
