from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
# Persistence of run packets moved from orchestrate-agents to run-pipeline
# (orchestrate-agents is now draft-only; run-pipeline writes the .ai/agent-runs/
# packet after dispatching the DAG). Tests check the executor's skill body.
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "run-pipeline" / "SKILL.md"
PROJECT_PATH = REPO_ROOT / ".ai" / "project.yaml"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
AGENT_RUNS_GITKEEP = REPO_ROOT / ".ai" / "agent-runs" / ".gitkeep"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_body_mentions_agent_runs_path():
    text = _read(SKILL_PATH)

    assert ".ai/agent-runs/" in text
    assert "new file only" in text.lower()


def test_skill_body_mentions_collision_suffix():
    text = _read(SKILL_PATH)

    assert "-N" in text or "-2" in text or "-3" in text


def test_project_yaml_lists_agent_runs_in_generated_files():
    data = yaml.safe_load(_read(PROJECT_PATH))

    assert ".ai/agent-runs" in data["boundaries"]["generated_files"]


def test_gitignore_contains_agent_runs():
    text = _read(GITIGNORE_PATH)
    lines = {line.strip() for line in text.splitlines()}

    assert ".ai/agent-runs/" in text
    assert (
        "!.ai/agent-runs/.gitkeep" in lines
        or "!/.ai/agent-runs/.gitkeep" in lines
    )


def test_gitkeep_is_tracked():
    assert AGENT_RUNS_GITKEEP.is_file()
