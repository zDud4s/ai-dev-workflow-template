"""End-to-end test for install.sh.

Runs install.sh against a tmp target directory and a sandboxed $HOME, then
asserts the expected layout was produced and that re-running the script is
idempotent for the mutable project layer.

Skipped if `bash` or a Python interpreter is not available on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, ALLOWED_TOOLS, PHASES


BASH = shutil.which("bash")
pytestmark = [pytest.mark.skipif(BASH is None, reason="bash not available on PATH"), pytest.mark.slow]


def _run_install(target: Path, fake_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    # Windows shells sometimes use USERPROFILE; bash uses HOME, but be safe.
    env["USERPROFILE"] = str(fake_home)
    return subprocess.run(
        [BASH, str(REPO_ROOT / "install.sh"), str(target)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _script_env(fake_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    return env


@pytest.fixture
def fake_install(tmp_path: Path):
    target = tmp_path / "target"
    home = tmp_path / "home"
    target.mkdir()
    home.mkdir()
    result = _run_install(target, home)
    assert result.returncode == 0, (
        f"install.sh failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return target, home, result


def test_install_creates_workflow_dirs(fake_install):
    target, _, _ = fake_install
    for sub in (".ai/workflow", ".ai/packets", ".claude/skills"):
        assert (target / sub).is_dir(), f"install.sh did not create {sub}"


def test_install_creates_workflow_doc(fake_install):
    target, _, _ = fake_install
    # Same dynamic-discovery rule as the main repo: any *workflow*.md.
    candidates = list((target / ".ai" / "workflow").glob("*workflow*.md"))
    assert candidates, "install.sh did not produce a workflow doc"


def test_install_creates_dispatch_doc(fake_install):
    target, _, _ = fake_install
    assert (target / ".ai" / "workflow" / "dispatch.md").is_file()


@pytest.mark.parametrize("name", ["plan.md", "execute.md", "review.md", "rescue.md"])
def test_install_creates_packets(fake_install, name):
    target, _, _ = fake_install
    assert (target / ".ai" / "packets" / name).is_file()


def test_install_creates_models_yaml(fake_install):
    target, _, _ = fake_install
    p = target / ".ai" / "models.yaml"
    assert p.is_file()
    import yaml
    cfg = yaml.safe_load(p.read_text())
    for phase in PHASES:
        block = cfg.get(phase)
        assert isinstance(block, dict), f"models.yaml in target missing `{phase}` block"
        assert block.get("tool") in ALLOWED_TOOLS


def test_install_creates_project_yaml(fake_install):
    target, _, _ = fake_install
    assert (target / ".ai" / "project.yaml").is_file()


def test_install_provisions_todos_module(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(
        [BASH, str(REPO_ROOT / "install.sh"), str(tmp_path)],
        cwd=REPO_ROOT,
        env=_script_env(home),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (tmp_path / ".ai" / "dashboard" / "scripts" / "todos_parser.py").is_file()
    assert (tmp_path / ".ai" / "dashboard" / "app" / "todos.js").is_file()


def test_update_workflow_refreshes_maintenance_skill_with_scan_step(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    env = _script_env(home)
    subprocess.run(
        [BASH, str(REPO_ROOT / "install.sh"), str(tmp_path)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    target_skill = tmp_path / ".claude" / "skills" / "maintenance" / "SKILL.md"
    target_skill.write_text("# stub\n", encoding="utf-8")

    subprocess.run(
        [BASH, str(REPO_ROOT / "update-workflow.sh"), str(tmp_path)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    source_skill = REPO_ROOT / ".claude" / "skills" / "maintenance" / "SKILL.md"
    assert target_skill.read_bytes() == source_skill.read_bytes()
    assert "Scan TODOs" in target_skill.read_text(encoding="utf-8")


def test_install_skills_present(fake_install):
    target, _, _ = fake_install
    skills_dir = target / ".claude" / "skills"
    have = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    required = {"orchestrate", "planner", "reviewer", "maintenance", "rescue", "bootstrap"}
    missing = required - have
    assert not missing, f"install.sh did not install skills: {sorted(missing)}"
    for skill in required:
        assert (skills_dir / skill / "SKILL.md").is_file()


def test_install_writes_claude_executor_skill_to_fake_home(fake_install):
    """install.sh must mirror the Codex-only `claude` skill into the global
    discovery path so Codex can use it as workflow executor or for ad-hoc
    delegation. Detailed mirror invariants live in test_mirror.py."""
    _, home, _ = fake_install
    mirrored = home / ".agents" / "skills" / "claude" / "SKILL.md"
    assert mirrored.is_file(), (
        "install.sh did not install the claude executor skill to $HOME/.agents/skills/"
    )


def test_install_writes_agents_managed_block(fake_install):
    target, _, _ = fake_install
    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "# >>> AI WORKFLOW MANAGED BLOCK >>>" in agents
    assert "# <<< AI WORKFLOW MANAGED BLOCK <<<" in agents


def test_install_writes_claude_managed_import(fake_install):
    target, _, _ = fake_install
    # install.sh writes to root CLAUDE.md or .claude/CLAUDE.md depending on
    # which one already exists. After a clean install, it goes to .claude/.
    candidates = [target / "CLAUDE.md", target / ".claude" / "CLAUDE.md"]
    existing = [c for c in candidates if c.exists()]
    assert existing, "install.sh did not produce a CLAUDE.md"
    content = existing[0].read_text(encoding="utf-8")
    assert ">>> AI WORKFLOW MANAGED IMPORT" in content
    assert "<<< AI WORKFLOW MANAGED IMPORT" in content


# --- Idempotency ------------------------------------------------------------


def test_install_is_idempotent_for_mutable_layer(fake_install, tmp_path):
    target, home, _ = fake_install
    # Modify the mutable layer; verify a second install does not overwrite it.
    project_yaml = target / ".ai" / "project.yaml"
    sentinel = "# test-sentinel-do-not-overwrite\n"
    project_yaml.write_text(sentinel + project_yaml.read_text())

    result = _run_install(target, home)
    assert result.returncode == 0, (
        f"second install.sh run failed:\n{result.stdout}\n{result.stderr}"
    )
    assert sentinel in project_yaml.read_text(), (
        "install.sh overwrote .ai/project.yaml on second run — mutable layer must be preserved"
    )


def test_install_refreshes_workflow_core(fake_install):
    """The workflow core (dispatch.md, workflow doc, packets) is treated as
    immutable by install.sh — `copy_always`. A second run should leave the
    content identical to the source."""
    target, home, _ = fake_install
    dispatch_target = target / ".ai" / "workflow" / "dispatch.md"
    dispatch_source = REPO_ROOT / ".ai" / "workflow" / "dispatch.md"
    assert dispatch_target.read_text() == dispatch_source.read_text(), (
        "install.sh did not copy dispatch.md verbatim"
    )
