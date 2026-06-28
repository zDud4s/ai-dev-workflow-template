"""Coverage guard for ``install.sh`` and ``update-workflow.sh``.

The installers ship a curated set of files into a downstream project (and mirror
skills into ``~/.agents/skills/``). Most files are referenced by an *explicit*
path list inside the scripts. So when a new feature adds a file under one of the
shipped directories and forgets to wire it into BOTH scripts, downstream
installs/updates silently miss it — the symptom the planner hit with
``auto-models.md`` and the agent-orchestration skills.

These tests make that failure loud, three ways:

* ``test_install_ships_every_source_file`` / ``test_update_ships_every_source_file``
  — *result-based*: run the script into a fresh sandboxed target and assert every
  expected source file actually lands there. Independent of HOW the script copies
  (explicit line, loop, or glob), so it can't be fooled.
* ``test_source_file_referenced_in_both_scripts`` — *text-based diagnostic*:
  points straight at the script that forgot the reference, with a faster failure.
* ``test_no_unclassified_source_file`` — forces every tracked file under a shipped
  directory into exactly one of two states: shipped, or listed in ``EXCLUDED``
  with a written reason. "I just forgot to add it" stops being possible.

To ship a new file: add a ``copy_if_*`` line (and, for a skill, the mkdir + the
two mirror loops) to both scripts. To deliberately NOT ship one: add it to
``EXCLUDED`` with a reason. Either way the tests stay green *on purpose*.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT


BASH = shutil.which("bash")
GIT = shutil.which("git")

INSTALL_SH = REPO_ROOT / "install.sh"
UPDATE_SH = REPO_ROOT / "update-workflow.sh"

# Whole-file integration suite: every test runs install.sh/update-workflow.sh as a
# subprocess (tens of seconds each). Excluded from the fast loop via -m "not slow".
pytestmark = pytest.mark.slow


# --- Contract: what the installers are responsible for placing in a target ---

# Top-level directories the installers own. Every git-tracked file under these
# (minus EXCLUDED, minus runtime placeholders) must be shipped to the target.
SHIPPED_DIRS = (
    ".claude/skills",
    ".ai/workflow",
    ".ai/packets",
    ".ai/dashboard",
)

# Loose project-state files the installers also place/merge (copy_if_missing or
# JSON/skeleton merge). Listed explicitly because they don't live under a single
# shipped dir we can sweep.
SHIPPED_LOOSE_FILES = (
    ".ai/project.yaml",
    ".ai/models.yaml",
    ".ai/memory.md",
    ".ai/decisions.md",
    ".claude/settings.json",
)

# Files that exist under a shipped dir but are intentionally NOT installed.
# Each entry needs a reason — that reason is the thing a future reviewer reads
# before deciding a newly-failing file belongs here vs. in the scripts.
EXCLUDED = {
    ".ai/packets/README.md": (
        "Human-facing doc explaining the packets dir; the installer ships only "
        "the schema templates (plan/execute/review/rescue), not their README."
    ),
    ".ai/dashboard/todos-config.json": (
        "User-mutable runtime config. todos_parser.load_config() defaults to "
        "{'auto_enabled': True} when absent and the dashboard owns writes via "
        "save_config(), so shipping it would risk clobbering a user's toggle."
    ),
}

# Source files matching these suffixes/dirs are runtime artifacts, not install
# payload — never expected in a fresh target.
def _is_runtime_placeholder(rel: str) -> bool:
    return rel.endswith("/.gitkeep") or rel.endswith(".gitkeep")


def _git_tracked_under(dirs: tuple[str, ...]) -> list[str]:
    """Git-tracked files (POSIX-relative) under the given repo-relative dirs."""
    out = subprocess.run(
        [GIT, "-C", str(REPO_ROOT), "ls-files", *dirs],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _expected_shipped_files() -> list[str]:
    """The canonical set of source files the installers must place in a target."""
    if GIT is None:
        return []
    tracked = _git_tracked_under(SHIPPED_DIRS)
    shipped = [
        rel
        for rel in tracked
        if rel not in EXCLUDED and not _is_runtime_placeholder(rel)
    ]
    shipped.extend(SHIPPED_LOOSE_FILES)
    return sorted(set(shipped))


EXPECTED_SHIPPED = _expected_shipped_files()

# Skill directories that ride the shared mirror loops: copied to .claude/skills/
# (source of truth), then mirrored to project .agents/skills/ AND global
# ~/.agents/skills/ so Codex can discover them. Claude-only skills (codex,
# agent-improver, agent-creator) are NOT here — they ship to .claude/skills/ only.
MIRRORED_SKILLS = (
    "bootstrap",
    "planner",
    "reviewer",
    "maintenance",
    "rescue",
    "orchestrate",
    "orchestrate-agents",
    "orchestrate-tdd",
    "run-pipeline",
    "synthesizer",
)


# --- Text-based reference diagnostics (no bash needed) -----------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# Skill lists are single-sourced in lib/skills.manifest (refactor #3). The
# installers no longer carry a literal `.claude/skills/<name>/SKILL.md` line per
# shared skill — they `source lib/workflow-lib.sh` and drive copy + mirror loops
# from the manifest groups. So "referenced" for those files means: the script
# reads the manifest AND the manifest lists that skill name in a group the loops
# consume. The result-based tests below (which actually run the installers) are
# the real guarantee the file lands; this text check just points at the wiring.
def _read_manifest_group(group: str) -> set[str]:
    manifest = REPO_ROOT / "lib" / "skills.manifest"
    if not manifest.exists():
        return set()
    names: set[str] = set()
    current = None
    for line in manifest.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        m = re.match(r"^#\s*===\s*([A-Za-z0-9_-]+)\s*===\s*$", stripped)
        if m:
            current = m.group(1)
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if current == group:
            names.add(stripped)
    return names


_MANIFEST_SHARED = _read_manifest_group("shared")
_MANIFEST_BRIDGE = _read_manifest_group("codex-bridge")


def _script_sources_manifest(script_text: str) -> bool:
    return "skills.manifest" in script_text and "workflow-lib.sh" in script_text


def _is_referenced(rel: str, script_text: str) -> bool:
    # app/*.js is shipped via a glob loop, not per-file lines. Treat the glob
    # over that dir as covering every .js under it.
    if rel.startswith(".ai/dashboard/app/") and rel.endswith(".js"):
        return ".ai/dashboard/app/" in script_text
    # styles/*.css ships via an analogous glob loop (sibling dashboard refactor).
    if rel.startswith(".ai/dashboard/styles/") and rel.endswith(".css"):
        return ".ai/dashboard/styles/" in script_text
    # server/*.py ships via an analogous glob loop (the serve.py decomposition
    # package — re-export modules serve.py imports at startup).
    if rel.startswith(".ai/dashboard/server/") and rel.endswith(".py"):
        return ".ai/dashboard/server/" in script_text
    # Shared / bridge skill SKILL.md files: driven by lib/skills.manifest loops.
    m = re.match(r"^\.(?:claude|agents)/skills/([^/]+)/SKILL\.md$", rel)
    if m and _script_sources_manifest(script_text):
        name = m.group(1)
        if name in _MANIFEST_SHARED or name in _MANIFEST_BRIDGE:
            return True
    # .claude/settings.json is merged inside lib/install_common.py now, not via a
    # literal line in the shell scripts.
    if rel == ".claude/settings.json" and "install_common.py" in script_text:
        return True
    return rel in script_text


def test_repo_has_tracked_shipped_files():
    """Guard against a silently-empty contract (e.g. git missing in CI): if the
    enumeration ever returns nothing, every parametrized test would vacuously
    pass. Fail loudly instead."""
    if GIT is None:
        pytest.skip("git not available on PATH")
    assert EXPECTED_SHIPPED, "no tracked shipped files discovered — enumeration broke"


@pytest.mark.skipif(GIT is None, reason="git not available on PATH")
@pytest.mark.parametrize("rel", EXPECTED_SHIPPED, ids=lambda r: r)
def test_source_file_referenced_in_both_scripts(rel: str):
    """Every shipped source file must be referenced by BOTH install.sh and
    update-workflow.sh. A file wired into only one drifts the two installers."""
    install_text = _read(INSTALL_SH)
    update_text = _read(UPDATE_SH)
    missing_from = [
        name
        for name, text in (("install.sh", install_text), ("update-workflow.sh", update_text))
        if not _is_referenced(rel, text)
    ]
    assert not missing_from, (
        f"`{rel}` is a tracked source file under a shipped directory but is not "
        f"referenced in: {', '.join(missing_from)}.\n"
        f"Either add a copy_if_* line for it to that script (and, for a skill, "
        f"the mkdir + both mirror loops), or add it to EXCLUDED in this test with "
        f"a reason."
    )


@pytest.mark.skipif(GIT is None, reason="git not available on PATH")
def test_no_unclassified_source_file():
    """Belt-and-suspenders: every tracked file under a shipped dir is either
    shipped or explicitly excluded. Catches a file that somehow slips past the
    per-file parametrization (e.g. a brand-new shipped dir)."""
    tracked = set(_git_tracked_under(SHIPPED_DIRS))
    classified = set(EXPECTED_SHIPPED) | set(EXCLUDED)
    unclassified = sorted(
        rel
        for rel in tracked
        if rel not in classified and not _is_runtime_placeholder(rel)
    )
    assert not unclassified, (
        "These tracked files live under a shipped directory but are neither "
        "shipped nor excluded:\n  " + "\n  ".join(unclassified) + "\n"
        "Wire each into install.sh + update-workflow.sh, or add it to EXCLUDED."
    )


@pytest.mark.skipif(GIT is None, reason="git not available on PATH")
@pytest.mark.parametrize("rel", sorted(EXCLUDED), ids=lambda r: r)
def test_excluded_file_still_exists(rel: str):
    """Stale-exclusion guard: if an EXCLUDED file is renamed/deleted, drop its
    entry instead of letting it rot and mask a future same-named file."""
    assert (REPO_ROOT / rel).exists(), (
        f"EXCLUDED lists `{rel}` but it no longer exists. Remove the stale entry."
    )


# --- Result-based tests: actually run the installers -------------------------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available on PATH")


def _run(script: Path, target: Path, fake_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    return subprocess.run(
        [BASH, str(script), str(target)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _assert_ok(result: subprocess.CompletedProcess, script: str) -> None:
    assert result.returncode == 0, (
        f"{script} failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "target"
    home = tmp_path / "home"
    target.mkdir()
    home.mkdir()
    return target, home


@pytestmark_bash
@pytest.mark.skipif(GIT is None, reason="git not available on PATH")
def test_install_ships_every_source_file(sandbox: tuple[Path, Path]):
    """After install.sh, every expected source file exists in the target. This is
    the test that fails the moment a new feature adds a file under a shipped dir
    without wiring it into install.sh."""
    target, home = sandbox
    _assert_ok(_run(INSTALL_SH, target, home), "install.sh")

    missing = [rel for rel in EXPECTED_SHIPPED if not (target / rel).is_file()]
    assert not missing, (
        "install.sh did not place these files in the target:\n  "
        + "\n  ".join(missing)
        + "\nAdd a copy_if_missing/copy_if_different line (and mkdir) to install.sh."
    )


@pytestmark_bash
@pytest.mark.skipif(GIT is None, reason="git not available on PATH")
def test_update_ships_every_source_file(sandbox: tuple[Path, Path]):
    """update-workflow.sh runs against an existing install. After it, every
    expected source file must still be present (none dropped, none missed)."""
    target, home = sandbox
    _assert_ok(_run(INSTALL_SH, target, home), "install.sh")
    _assert_ok(_run(UPDATE_SH, target, home), "update-workflow.sh")

    missing = [rel for rel in EXPECTED_SHIPPED if not (target / rel).is_file()]
    assert not missing, (
        "update-workflow.sh left these files missing in the target:\n  "
        + "\n  ".join(missing)
        + "\nAdd a copy_if_different line (and mkdir) to update-workflow.sh."
    )


@pytestmark_bash
def test_install_then_update_is_idempotent(sandbox: tuple[Path, Path]):
    """A no-op update (right after install) must not error and must keep
    immutable-core files byte-identical to source."""
    target, home = sandbox
    _assert_ok(_run(INSTALL_SH, target, home), "install.sh")
    _assert_ok(_run(UPDATE_SH, target, home), "update-workflow.sh")

    for rel in (".ai/workflow/workflow.md", ".ai/workflow/auto-models.md"):
        assert (target / rel).read_text(encoding="utf-8") == (
            REPO_ROOT / rel
        ).read_text(encoding="utf-8"), f"{rel} drifted from source after update"


@pytestmark_bash
@pytest.mark.parametrize("skill", MIRRORED_SKILLS, ids=lambda s: s)
def test_install_mirrors_skill_everywhere(sandbox: tuple[Path, Path], skill: str):
    """Mirrored skills must land in all three discovery paths: .claude/skills/
    (Claude source), project .agents/skills/, and global ~/.agents/skills/
    (Codex). A skill missing from any path is invisible to one of the runtimes."""
    target, home = sandbox
    _assert_ok(_run(INSTALL_SH, target, home), "install.sh")

    paths = {
        ".claude/skills": target / ".claude" / "skills" / skill / "SKILL.md",
        "project .agents/skills": target / ".agents" / "skills" / skill / "SKILL.md",
        "global ~/.agents/skills": home / ".agents" / "skills" / skill / "SKILL.md",
    }
    missing = [label for label, p in paths.items() if not p.is_file()]
    assert not missing, (
        f"skill `{skill}` not mirrored to: {', '.join(missing)}. "
        f"Add it to the mkdir block, the .claude/skills copy block, and the "
        f"project + global mirror `for skill in ...` loops in install.sh."
    )


@pytestmark_bash
@pytest.mark.parametrize("skill", MIRRORED_SKILLS, ids=lambda s: s)
def test_update_mirrors_skill_everywhere(sandbox: tuple[Path, Path], skill: str):
    target, home = sandbox
    _assert_ok(_run(INSTALL_SH, target, home), "install.sh")
    _assert_ok(_run(UPDATE_SH, target, home), "update-workflow.sh")

    paths = {
        ".claude/skills": target / ".claude" / "skills" / skill / "SKILL.md",
        "project .agents/skills": target / ".agents" / "skills" / skill / "SKILL.md",
        "global ~/.agents/skills": home / ".agents" / "skills" / skill / "SKILL.md",
    }
    missing = [label for label, p in paths.items() if not p.is_file()]
    assert not missing, (
        f"skill `{skill}` not mirrored by update-workflow.sh to: "
        f"{', '.join(missing)}."
    )
