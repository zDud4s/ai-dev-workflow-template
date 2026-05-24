"""Structural invariants: required files and dirs exist; frontmatter is valid."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    AGENTS_SKILLS_DIR,
    CLAUDE_SKILLS_DIR,
    PACKETS_DIR,
    PHASES,
    WORKFLOW_DIR,
    discover_skill_dirs,
    parse_frontmatter,
    repo_path,
)


# --- Top-level scaffold ------------------------------------------------------


def test_scaffold_dirs_exist():
    assert WORKFLOW_DIR.is_dir(), ".ai/workflow/ must exist"
    assert PACKETS_DIR.is_dir(), ".ai/packets/ must exist"
    assert CLAUDE_SKILLS_DIR.is_dir(), ".claude/skills/ must exist"
    assert AGENTS_SKILLS_DIR.is_dir(), ".agents/skills/ must exist (repo copy)"


def test_core_config_files_exist():
    for name in ("memory.md", "decisions.md"):
        assert repo_path(".ai", name).is_file(), f".ai/{name} missing"
    # models.yaml and project.yaml are gitignored working files; the .template
    # siblings are the source of truth shipped in git. Accept either.
    for name in ("models.yaml", "project.yaml"):
        filled = repo_path(".ai", name)
        template = repo_path(".ai", f"{name}.template")
        assert filled.is_file() or template.is_file(), (
            f"Neither .ai/{name} nor .ai/{name}.template exists"
        )


def test_install_scripts_exist_and_executable_flag_set():
    for name in ("install.sh", "update-workflow.sh"):
        p = repo_path(name)
        assert p.is_file(), f"{name} missing at repo root"


# --- Workflow doc + dispatch -------------------------------------------------


def test_workflow_doc_present(workflow_doc):
    assert workflow_doc.is_file()
    # Should not be empty.
    assert workflow_doc.read_text(encoding="utf-8").strip(), "workflow doc is empty"


def test_workflow_doc_is_unique():
    """Exactly one workflow doc in .ai/workflow/ (excluding dispatch + agents
    block). Two side-by-side (e.g. a leftover `claude-workflow.md` plus a new
    `workflow.md` after rename) would mask stale references."""
    candidates = [
        p
        for p in WORKFLOW_DIR.glob("*.md")
        if p.name not in {"dispatch.md", "agents-block.md", "auto-models.md"}
    ]
    assert len(candidates) == 1, (
        f"Expected exactly one workflow doc in .ai/workflow/, "
        f"found {[p.name for p in candidates]}"
    )


def test_dispatch_doc_present():
    assert (WORKFLOW_DIR / "dispatch.md").is_file()


def test_agents_block_present():
    assert (WORKFLOW_DIR / "agents-block.md").is_file()


# --- Packets -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["plan.md", "execute.md", "review.md", "rescue.md"])
def test_packet_template_exists(name):
    assert (PACKETS_DIR / name).is_file(), f"missing packet template: {name}"


# --- Skills ------------------------------------------------------------------


def test_skill_dirs_non_empty():
    dirs = discover_skill_dirs()
    assert dirs, ".claude/skills/ has no skill directories"


@pytest.mark.parametrize("skill_dir", discover_skill_dirs(), ids=lambda p: p.name)
def test_skill_has_skill_md(skill_dir: Path):
    assert (skill_dir / "SKILL.md").is_file(), f"{skill_dir.name} missing SKILL.md"


@pytest.mark.parametrize("skill_dir", discover_skill_dirs(), ids=lambda p: p.name)
def test_skill_frontmatter_valid(skill_dir: Path):
    fm = parse_frontmatter(skill_dir / "SKILL.md")
    assert fm, f"{skill_dir.name}: missing or invalid YAML frontmatter"
    assert "name" in fm, f"{skill_dir.name}: frontmatter missing `name`"
    assert "description" in fm, f"{skill_dir.name}: frontmatter missing `description`"


@pytest.mark.parametrize("skill_dir", discover_skill_dirs(), ids=lambda p: p.name)
def test_skill_name_matches_dir(skill_dir: Path):
    fm = parse_frontmatter(skill_dir / "SKILL.md")
    assert fm.get("name") == skill_dir.name, (
        f"frontmatter name `{fm.get('name')}` != dir name `{skill_dir.name}`"
    )


def test_expected_phase_skills_exist():
    """Each workflow phase must have a corresponding skill so dispatch can
    inline the skill content into prompts. The `execute` phase is excluded
    because its skill is named after the executor tool (e.g. `codex`)."""
    # Phase → skill directory name. Two phases have differently-named skills.
    phase_to_skill = {
        "plan": "planner",
        "review": "reviewer",
        "rescue": "rescue",
        "maintenance": "maintenance",
        "bootstrap": "bootstrap",
    }
    have = {p.name for p in discover_skill_dirs()}
    missing = {
        phase: skill
        for phase, skill in phase_to_skill.items()
        if skill not in have
    }
    assert not missing, f"missing skills for phases: {missing}"


def test_orchestrate_skill_present():
    assert (CLAUDE_SKILLS_DIR / "orchestrate" / "SKILL.md").is_file()


# --- Codex-only skills -------------------------------------------------------


def test_claude_executor_skill_in_repo():
    """`.agents/skills/claude/SKILL.md` is the Codex-only executor skill —
    symmetric counterpart of `.claude/skills/codex/`. install.sh and
    update-workflow.sh mirror it into ~/.agents/skills/claude/ so Codex can
    discover it (both as workflow executor and for ad-hoc delegation)."""
    p = AGENTS_SKILLS_DIR / "claude" / "SKILL.md"
    assert p.is_file()
    fm = parse_frontmatter(p)
    assert fm.get("name") == "claude"
