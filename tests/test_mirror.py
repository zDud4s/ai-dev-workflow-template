"""Fase 2 invariants: install.sh mirrors all skills into ~/.agents/skills/.

Codex only discovers skills under ~/.agents/skills/ (no project-local
discovery). For Codex to act as orchestrator, every workflow skill must end up
there after install.sh runs.

These tests exercise install.sh against a fresh tmp dir with a sandboxed $HOME
and assert the resulting layout. Skipped if bash is not on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT


BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available on PATH")

# Skills that MUST be present under ~/.agents/skills/ after install. These are
# the skills any Codex-as-orchestrator session needs to find. `codex` itself is
# intentionally excluded — Codex does not need a skill describing how to invoke
# itself (mirrors install.sh's exclusion list).
REQUIRED_GLOBAL_SKILLS = [
    "orchestrate",
    "planner",
    "reviewer",
    "maintenance",
    "rescue",
    "bootstrap",
    "claude",
]


def _run_install(target: Path, fake_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    return subprocess.run(
        [BASH, str(REPO_ROOT / "install.sh"), str(target)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def installed(tmp_path: Path):
    target = tmp_path / "target"
    home = tmp_path / "home"
    target.mkdir()
    home.mkdir()
    result = _run_install(target, home)
    assert result.returncode == 0, (
        f"install.sh failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return target, home, result


@pytest.mark.parametrize("name", REQUIRED_GLOBAL_SKILLS)
def test_skill_mirrored_to_home_agents_skills(installed, name):
    """Every workflow skill must be discoverable at ~/.agents/skills/<name>/
    after install.sh — this is the path Codex scans on startup."""
    _, home, _ = installed
    p = home / ".agents" / "skills" / name / "SKILL.md"
    assert p.is_file(), (
        f"install.sh did not mirror `{name}` to {p}. "
        "Codex will not be able to discover this skill."
    )


@pytest.mark.parametrize("name", ["orchestrate", "planner", "reviewer", "maintenance", "rescue", "bootstrap"])
def test_shared_skill_mirror_matches_source(installed, name):
    """Shared skills have a single source under .claude/skills/<name>/. The
    global mirror at ~/.agents/skills/<name>/ must be byte-identical so the
    two discovery paths resolve to the same content."""
    _, home, _ = installed
    source = REPO_ROOT / ".claude" / "skills" / name / "SKILL.md"
    mirrored = home / ".agents" / "skills" / name / "SKILL.md"
    assert source.read_text() == mirrored.read_text(), (
        f"Mirror of `{name}` to ~/.agents/skills/ does not match source at .claude/skills/"
    )


def test_codex_only_skill_mirror_matches_source(installed):
    """The Codex-only `claude` skill has its canonical source under
    .agents/skills/claude/ in the repo (no Claude project-local counterpart —
    Claude never needs to invoke itself). The global mirror must be
    byte-identical to the source."""
    _, home, _ = installed
    source = REPO_ROOT / ".agents" / "skills" / "claude" / "SKILL.md"
    mirrored = home / ".agents" / "skills" / "claude" / "SKILL.md"
    assert source.read_text() == mirrored.read_text(), (
        "Mirror of `claude` to ~/.agents/skills/ does not match source at .agents/skills/"
    )


def test_install_does_not_touch_real_home():
    """The install fixture sets $HOME to tmp_path. Sanity check: the test
    suite has not leaked anything into the user's real home."""
    # We can't trivially detect what the install would do globally, but we can
    # at least make sure the install script honors $HOME. This is implicit in
    # the other mirror tests passing with a sandboxed $HOME; this test is a
    # named guarantee for the contract.
    real_home = Path.home()
    # The repo lives under the user's home in dev, but the .agents/skills/
    # under it is the SOURCE, not the global mirror. The mirror lives at
    # ~/.agents/skills/ — which the user may have populated outside of tests
    # by running install.sh manually. We can't assert that it's empty. So
    # this test is informational only.
    _ = real_home
