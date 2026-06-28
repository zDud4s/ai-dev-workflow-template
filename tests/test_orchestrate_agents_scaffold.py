from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "orchestrate-agents" / "SKILL.md"
SYNTHESIZER_PATH = REPO_ROOT / ".claude" / "skills" / "synthesizer" / "SKILL.md"
MODELS_PATH = REPO_ROOT / ".ai" / "models.yaml"
WORKFLOW_PATH = REPO_ROOT / ".ai" / "workflow" / "workflow.md"
ORCHESTRATE_PATH = REPO_ROOT / ".claude" / "skills" / "orchestrate" / "SKILL.md"
AGENT_PLANNER_PATH = REPO_ROOT / ".claude" / "skills" / "agent-planner" / "SKILL.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(path: Path) -> dict:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", _read(path), re.DOTALL)
    assert match, f"{path} is missing YAML frontmatter"
    return yaml.safe_load(match.group(1)) or {}


def test_skill_frontmatter():
    """After the merge with agent-planner, the skill needs `Write` (saves the
    drafted YAML to .ai/local/pipelines/<slug>.yaml) alongside the original tools.
    """
    frontmatter = _frontmatter(SKILL_PATH)

    assert frontmatter["name"] == "orchestrate-agents"
    assert frontmatter.get("description")
    tools = [t.strip() for t in str(frontmatter["tools"]).split(",")]
    for required in ("Read", "Glob", "Grep", "Bash", "Write", "Task"):
        assert required in tools, f"missing tool: {required}"


def test_models_yaml_has_no_orchestrate_agents_block():
    """After merging agent-planner into orchestrate-agents (draft runs inline
    in the controller session), the `orchestrate_agents` block in models.yaml
    is removed entirely. `synthesize` and `maintenance` live under
    `run_pipeline:`.
    """
    data = yaml.safe_load(_read(MODELS_PATH))

    assert "orchestrate_agents" not in data, (
        "orchestrate_agents block was dropped when agent-planner merged into "
        "orchestrate-agents (planning runs inline now)"
    )


def test_agent_planner_skill_removed():
    """`agent-planner` was merged into `orchestrate-agents`. The standalone
    skill should no longer exist in the discovery path.
    """
    assert not AGENT_PLANNER_PATH.exists(), (
        "agent-planner/SKILL.md must be removed after the merge"
    )


def test_workflow_md_mentions_agent_orchestrator():
    assert "orchestrate-agents" in _read(WORKFLOW_PATH)


def test_orchestrate_skill_unchanged_vs_HEAD():
    """The code-task orchestrator skill stays free of agent-orchestration tokens
    and the new pipeline tokens — they belong to other skills.
    """
    text = _read(ORCHESTRATE_PATH)

    for leaked in (
        "orchestrate-agents",
        "run-pipeline",
        "pipeline_dispatch",
        "pipeline_synthesis",
    ):
        assert leaked not in text


def test_synthesizer_skill_exists():
    assert SYNTHESIZER_PATH.exists()
    frontmatter = _frontmatter(SYNTHESIZER_PATH)

    assert frontmatter["name"] == "synthesizer"


def test_models_yaml_points_at_new_skills():
    """After the merge: only `run_pipeline.synthesize.skill == synthesizer`
    survives (executor for synthesize-mode pipelines). Draft planning is now
    inline in orchestrate-agents.
    """
    data = yaml.safe_load(_read(MODELS_PATH))

    assert data["run_pipeline"]["synthesize"]["skill"] == "synthesizer"
