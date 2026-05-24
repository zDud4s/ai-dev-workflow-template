"""Shared fixtures and helpers for the workflow scaffold test suite.

The tests assert *invariants* of the scaffold, not hardcoded filenames. So a
rename like `claude-workflow.md` -> `workflow.md` only fails the tests if a
reference somewhere is left stale.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable

import pytest
import yaml

# Stop the dashboard's auto-improver from spawning real `claude -p` subprocesses
# while the tests run. Each subprocess opens a fresh Claude Code chat session
# and pollutes the user's history with "OUTPUT FORMAT (STRICT)" prompts. The
# env var is read inside `_load_improver_config` (.ai/dashboard/serve.py) and
# forces `enabled=False`. Set before any test imports serve.py.
os.environ.setdefault("AI_WORKFLOW_DISABLE_IMPROVER", "1")


REPO_ROOT = Path(__file__).resolve().parent.parent

# Tests that load `serve.py` via `importlib.util.spec_from_file_location` hit
# `import pty_session as _pty_session` inside serve.py (sibling module under
# .ai/dashboard/). Without this insert, those tests fail with
# `ModuleNotFoundError: No module named 'pty_session'`. Files using the simpler
# `sys.path.insert(...); import serve` pattern do it themselves; this conftest
# covers the importlib pattern too so the same fix lands once for all suites.
sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))

WORKFLOW_DIR = REPO_ROOT / ".ai" / "workflow"
PACKETS_DIR = REPO_ROOT / ".ai" / "packets"
CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"

# Phases the workflow contract talks about. Tests use this to drive
# parameterization without hardcoding it per assertion.
PHASES = ("plan", "execute", "review", "rescue", "maintenance", "bootstrap")
ALLOWED_TOOLS = ("claude", "codex")

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def repo_path(*parts: str) -> Path:
    return REPO_ROOT.joinpath(*parts)


def parse_frontmatter(path: Path) -> dict:
    """Return YAML frontmatter from a markdown file, or {} if none."""
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


def discover_workflow_doc() -> Path:
    """Return the canonical workflow doc inside `.ai/workflow/`.

    Today that's `claude-workflow.md`. After the planned refactor it may be
    `workflow.md`. We accept any non-dispatch, non-agents-block markdown.
    """
    candidates = [
        p
        for p in WORKFLOW_DIR.glob("*.md")
        if p.name not in {"dispatch.md", "agents-block.md"}
    ]
    if not candidates:
        raise FileNotFoundError("No workflow doc found in .ai/workflow/")
    # Prefer one named workflow.md or claude-workflow.md if both exist.
    for preferred in ("workflow.md", "claude-workflow.md"):
        for p in candidates:
            if p.name == preferred:
                return p
    return candidates[0]


def discover_skill_dirs() -> list[Path]:
    if not CLAUDE_SKILLS_DIR.exists():
        return []
    return sorted(p for p in CLAUDE_SKILLS_DIR.iterdir() if p.is_dir())


def iter_workflow_markdown() -> Iterable[Path]:
    """Markdown files that participate in the workflow contract.

    Excludes the mutable project layer (memory, decisions) and any task
    instances under .ai/plans or .ai/specs.
    """
    yield from WORKFLOW_DIR.glob("*.md")
    yield from PACKETS_DIR.glob("*.md")
    for skill_dir in discover_skill_dirs():
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            yield skill_file
    bridge = AGENTS_SKILLS_DIR / "call-claude" / "SKILL.md"
    if bridge.exists():
        yield bridge


# Pytest fixtures -------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def workflow_doc() -> Path:
    return discover_workflow_doc()


@pytest.fixture(scope="session")
def skill_dirs() -> list[Path]:
    return discover_skill_dirs()


def _load_yaml_with_template_fallback(name: str) -> dict:
    """Load .ai/<name>; fall back to .ai/<name>.template if the working file
    is absent. The working file (filled in per project) is gitignored; the
    template ships in git as the schema-bearing source of truth."""
    filled = REPO_ROOT / ".ai" / name
    template = REPO_ROOT / ".ai" / f"{name}.template"
    path = filled if filled.exists() else template
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def models_config() -> dict:
    return _load_yaml_with_template_fallback("models.yaml")


@pytest.fixture(scope="session")
def project_config() -> dict:
    return _load_yaml_with_template_fallback("project.yaml")
