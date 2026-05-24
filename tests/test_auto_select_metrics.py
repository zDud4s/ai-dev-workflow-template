"""Metrics writer tests (spec section: Phase 2 - adaptive scorer / Metrics file)."""
from __future__ import annotations

import pytest

from conftest import CLAUDE_SKILLS_DIR, REPO_ROOT

ORCHESTRATE = CLAUDE_SKILLS_DIR / "orchestrate" / "SKILL.md"
GITIGNORE = REPO_ROOT / ".gitignore"


@pytest.fixture(scope="module")
def orchestrate_text():
    return ORCHESTRATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def gitignore_text():
    return GITIGNORE.read_text(encoding="utf-8")


def test_orchestrate_has_metrics_section(orchestrate_text):
    """orchestrate skill must document the metrics writer (spec PR 2)."""
    assert "## Metrics logging" in orchestrate_text, (
        "orchestrate skill must document `## Metrics logging` section"
    )


def test_metrics_targets_jsonl_file(orchestrate_text):
    """Metrics writer targets .ai/metrics.jsonl, gitignored, append-only."""
    section = orchestrate_text.split("## Metrics logging", 1)[1]
    assert ".ai/metrics.jsonl" in section, (
        "metrics section must name the target file .ai/metrics.jsonl"
    )
    assert "append" in section.lower(), (
        "metrics section must specify append-only semantics"
    )
    assert "gitignored" in section.lower() or "git-ignored" in section.lower(), (
        "metrics section must specify the file is gitignored"
    )


def test_metrics_runs_regardless_of_auto_select(orchestrate_text):
    """PR 2's writer must run even when auto_select.enabled is false."""
    section = orchestrate_text.split("## Metrics logging", 1)[1]
    assert "regardless of `auto_select.enabled`" in section, (
        "metrics section must state it runs regardless of auto_select.enabled"
    )


def test_metrics_schema_documents_required_fields(orchestrate_text):
    """Schema must include the fields the PR 3 scorer reads."""
    section = orchestrate_text.split("## Metrics logging", 1)[1]
    required = (
        "ts",
        "task_slug",
        "phase",
        "tool",
        "model",
        "reasoning_effort",
        "size",
        "risk",
        "budget",
        "exit_code",
        "duration_ms",
        "handoff_complete",
        "review_verdict",
        "retries",
        "tokens_in",
        "tokens_out",
    )
    for field in required:
        assert f'"{field}"' in section or f"`{field}`" in section, (
            f"metrics schema missing field reference: {field!r}"
        )


def test_metrics_writer_is_observability_not_control_flow(orchestrate_text):
    """Failures writing metrics must not abort the pipeline."""
    section = orchestrate_text.split("## Metrics logging", 1)[1]
    assert "never abort" in section.lower() or "observability" in section.lower(), (
        "metrics section must declare observability semantics (failures do not abort)"
    )


def test_gitignore_excludes_metrics_jsonl(gitignore_text):
    """.gitignore must exclude .ai/metrics.jsonl (spec PR 2 acceptance)."""
    assert ".ai/metrics.jsonl" in gitignore_text, (
        ".gitignore must contain `.ai/metrics.jsonl`"
    )
