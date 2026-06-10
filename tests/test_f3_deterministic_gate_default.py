"""Regression tests for deterministic-gate default workflow policy."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATE = REPO_ROOT / ".claude" / "skills" / "orchestrate" / "SKILL.md"
PLANNER = REPO_ROOT / ".claude" / "skills" / "planner" / "SKILL.md"
WORKFLOW = REPO_ROOT / ".ai" / "workflow" / "workflow.md"


def _read_lower(path: Path) -> str:
    return path.read_text(encoding="utf-8").lower()


def test_orchestrate_defaults_codetask_to_deterministic_gate() -> None:
    assert (
        "medium/large code changes that are tdd-able default to the deterministic gate"
        in _read_lower(ORCHESTRATE)
    )


def test_orchestrate_gate_exitcode_is_ship_decision() -> None:
    assert (
        "the gate command's exit code, re-run independently by the orchestrator, is the recorded ship/no-ship decision"
        in _read_lower(ORCHESTRATE)
    )


def test_review_advisory_where_gate_exists_and_hardgates_authoritative() -> None:
    text = _read_lower(ORCHESTRATE)
    assert "where a deterministic gate exists and passed, the reviewer verdict is advisory" in text
    assert "the reviewer's hard-gate checks remain authoritative" in text
    assert "tasks without a deterministic gate keep the blocking reviewer verdict" in text


def test_planner_emits_changetype_and_tddable() -> None:
    text = PLANNER.read_text(encoding="utf-8")
    assert "Change type:" in text
    assert "TDD-able:" in text


def test_workflow_rule6_reflects_deterministic_gate() -> None:
    assert (
        "for code changes the deterministic gate is the ship/no-ship decision; llm review is advisory"
        in _read_lower(WORKFLOW)
    )
