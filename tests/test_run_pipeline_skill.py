"""Static-lint over .claude/skills/run-pipeline/SKILL.md frontmatter + body."""
from __future__ import annotations
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO_ROOT / ".claude" / "skills" / "run-pipeline" / "SKILL.md"


def _frontmatter(path: pathlib.Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "skill must start with YAML frontmatter"
    end = text.index("\n---\n", 4)
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_skill_file_exists() -> None:
    assert SKILL.is_file()


def test_frontmatter_is_valid() -> None:
    fm = _frontmatter(SKILL)
    assert fm["name"] == "run-pipeline"
    assert "description" in fm and fm["description"]
    assert "tools" in fm
    for tool in ("Read", "Glob", "Grep", "Bash", "Task"):
        assert tool in fm["tools"], f"missing tool '{tool}' in tools allowlist"


def test_body_mentions_pipelines_dir() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert ".ai/pipelines/" in text


def test_body_documents_three_output_modes() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for mode in ("synthesize", "passthrough", "per-agent"):
        assert mode in text


def test_body_documents_three_node_statuses() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for st in ("completed", "failed", "skipped"):
        assert st in text


def test_body_mentions_metric_phases() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for phase in ("pipeline_dispatch", "pipeline_synthesis"):
        assert phase in text


def test_body_documents_memory_consolidation_trigger() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "run_pipeline.maintenance" in text or "consolidation_threshold_lines" in text


def test_body_mentions_ancestor_char_limit_constant() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "ANCESTOR_OUTPUT_CHAR_LIMIT" in text
    assert re.search(r"ANCESTOR_OUTPUT_CHAR_LIMIT\s*=\s*\d+", text)


def test_body_references_dispatch_md_once() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert ".ai/workflow/dispatch.md" in text
    assert text.count(".ai/workflow/dispatch.md") <= 2
