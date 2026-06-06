"""Install/update coverage for the project-owned agent-creator skill."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT


BASH = shutil.which("bash")
pytestmark = [pytest.mark.skipif(BASH is None, reason="bash not available on PATH"), pytest.mark.slow]


def _run_script(script: str, target: Path, fake_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    return subprocess.run(
        [BASH, str(REPO_ROOT / script), str(target)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_ok(result: subprocess.CompletedProcess, script: str) -> None:
    assert result.returncode == 0, (
        f"{script} failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture
def target_and_home(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "target"
    home = tmp_path / "home"
    target.mkdir()
    home.mkdir()
    return target, home


def test_install_provisions_agent_creator_skill(target_and_home: tuple[Path, Path]):
    target, home = target_and_home

    result = _run_script("install.sh", target, home)
    _assert_ok(result, "install.sh")

    skill = target / ".claude" / "skills" / "agent-creator" / "SKILL.md"
    template = skill.parent / "references" / "agent-template.md"
    assert skill.is_file()
    assert template.is_file()
    assert skill.read_text(encoding="utf-8") == (
        REPO_ROOT / ".claude" / "skills" / "agent-creator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert template.read_text(encoding="utf-8") == (
        REPO_ROOT
        / ".claude"
        / "skills"
        / "agent-creator"
        / "references"
        / "agent-template.md"
    ).read_text(encoding="utf-8")


def test_install_is_idempotent_for_agent_creator(target_and_home: tuple[Path, Path]):
    target, home = target_and_home
    _assert_ok(_run_script("install.sh", target, home), "install.sh")

    skill = target / ".claude" / "skills" / "agent-creator" / "SKILL.md"
    sentinel = "\n<!-- keep local customization -->\n"
    skill.write_text(skill.read_text(encoding="utf-8") + sentinel, encoding="utf-8")

    _assert_ok(_run_script("install.sh", target, home), "install.sh")

    assert sentinel in skill.read_text(encoding="utf-8")


def test_update_workflow_refreshes_agent_creator(target_and_home: tuple[Path, Path]):
    target, home = target_and_home
    _assert_ok(_run_script("install.sh", target, home), "install.sh")

    skill = target / ".claude" / "skills" / "agent-creator" / "SKILL.md"
    template = skill.parent / "references" / "agent-template.md"
    skill.write_text("stale skill\n", encoding="utf-8")
    template.write_text("stale template\n", encoding="utf-8")

    _assert_ok(_run_script("update-workflow.sh", target, home), "update-workflow.sh")

    assert skill.read_text(encoding="utf-8") == (
        REPO_ROOT / ".claude" / "skills" / "agent-creator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert template.read_text(encoding="utf-8") == (
        REPO_ROOT
        / ".claude"
        / "skills"
        / "agent-creator"
        / "references"
        / "agent-template.md"
    ).read_text(encoding="utf-8")


def test_agent_improver_no_longer_delegates_to_plugin_creator():
    text = (
        REPO_ROOT / ".claude" / "skills" / "agent-improver" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "dispatch the existing `agent-creator` agent via the `Agent` tool" not in text
    assert "invoke the project `agent-creator` skill via the Skill tool" in text
    assert "| `agent-creator` project skill | Creates new agents from a spec |" in text
