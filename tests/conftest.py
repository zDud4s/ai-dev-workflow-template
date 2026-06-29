"""Shared fixtures and helpers for the workflow scaffold test suite.

The tests assert *invariants* of the scaffold, not hardcoded filenames. So a
rename like `claude-workflow.md` -> `workflow.md` only fails the tests if a
reference somewhere is left stale.
"""

from __future__ import annotations

import os
import re
import socket
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

# Tests that load `serve.py` via `importlib.util.spec_from_file_location` need
# two dirs on sys.path: `.ai/dashboard` so `import serve` and `from server
# import …` resolve (dashboard-only helpers now live in the server/ package),
# and `.ai/scripts` so the bare workflow-helper imports (`import todos_parser`
# / `import auto_select_scorer` / `from pipeline_schema import …`) resolve.
sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
sys.path.insert(0, str(REPO_ROOT / ".ai" / "scripts"))

WORKFLOW_DIR = REPO_ROOT / ".ai" / "workflow"
PACKETS_DIR = REPO_ROOT / ".ai" / "packets"
CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"

# Phases the workflow contract talks about. Tests use this to drive
# parameterization without hardcoding it per assertion.
PHASES = ("plan", "execute", "review", "rescue", "maintenance", "bootstrap")
ALLOWED_TOOLS = ("claude", "codex")

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers from tracked code.

    `pytest.ini` is gitignored (local-only test config), so the marker
    registration lives here in conftest.py — which IS tracked — to stay in
    lockstep with the `@pytest.mark.slow` usages in the tracked test modules.
    Otherwise a checkout without the local pytest.ini hits `--strict-markers`.
    """
    config.addinivalue_line(
        "markers",
        'slow: integration tests that run install.sh/update-workflow.sh/mirror '
        'subprocesses (tens of seconds each); excluded from the fast loop via '
        '-m "not slow"',
    )
    config.addinivalue_line(
        "markers",
        "browser: playwright/dashboard UI tests that need a live dashboard on "
        ":8766; skipped at collection when it is unreachable (avoids a Windows "
        "playwright teardown hang) — see pytest_collection_modifyitems",
    )


def _dashboard_reachable(host: str = "localhost", port: int = 8766, timeout: float = 0.3) -> bool:
    """True if something is accepting connections on the dashboard port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `browser` tests when no dashboard is live on :8766.

    Those tests launch real Chromium and `page.goto(:8766)`. When the dashboard
    is down (the usual case in the fast/pre-push loop and CI) the tests used to
    `pytest.skip()` *inside* `page.goto`, leaving a pending playwright async task
    whose teardown hangs / raises KeyboardInterrupt on Windows — poisoning the
    whole run's exit code to 2 even though every test passed. Skipping at
    collection means playwright never launches, so there is nothing to tear down.
    A live dashboard (developer running them deliberately) lets them run normally.
    """
    if _dashboard_reachable():
        return
    skip = pytest.mark.skip(
        reason="dashboard not reachable on :8766; browser tests skipped before launching playwright"
    )
    for item in items:
        if item.get_closest_marker("browser"):
            item.add_marker(skip)


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
