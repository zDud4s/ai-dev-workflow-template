"""Regression tests for the workflow-version bootstrap fixes.

Three bugs were fixed together; this file pins all three:

  1. Gating chicken-and-egg — the dashboard's "Apply update" button is gated on
     ``has_updates``, which used to require ``current_sha is not None``. A fresh
     install has no recorded ``.ai/workflow/.version`` (current_sha is None), so
     the button stayed disabled forever — yet the only writer of that file is a
     *successful* apply. The pure decision now lives in
     ``WorkflowSettingsRoutes._compute_has_updates`` and treats an unversioned
     install as updatable (bootstrap apply).

  2. Install stamp — nothing used to write ``.version`` at install/update time
     (only the dashboard, post-apply). ``install_common.stamp_workflow_version``
     now records the template's HEAD sha so a fresh install starts versioned.

  3. Hygiene — the template repo tracks its own ``.ai/`` so ``.version`` (a
     per-machine runtime artifact) must be ignored here explicitly. (Consumer
     projects already ignore all of ``.ai/`` via the managed block.)
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Fix 1: has_updates decision -----------------------------------------------

from server.handlers.workflow import WorkflowSettingsRoutes  # noqa: E402

_has_updates = WorkflowSettingsRoutes._compute_has_updates


def test_unversioned_install_is_updatable():
    # Fresh install: no .version recorded yet. Must be offered an apply so the
    # sha can be bootstrapped — this is the bug that left the button disabled.
    assert _has_updates("a" * 40, None, False) is True


def test_up_to_date_when_shas_match():
    assert _has_updates("a" * 40, "a" * 40, False) is False


def test_behind_when_shas_differ():
    assert _has_updates("a" * 40, "b" * 40, False) is True


def test_template_repo_never_updatable():
    # Serving from the template checkout: HEAD *is* upstream, so never offer an
    # update — even when current_sha is None.
    assert _has_updates("a" * 40, None, True) is False
    assert _has_updates("a" * 40, "b" * 40, True) is False


def test_no_upstream_sha_not_updatable():
    # Clone/rev-parse failed upstream — can't compute a meaningful diff.
    assert _has_updates("", None, False) is False


# --- Fix 2: install-time version stamp -----------------------------------------

def _load_install_common():
    path = REPO_ROOT / "lib" / "install_common.py"
    spec = importlib.util.spec_from_file_location("install_common_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_stamp_writes_template_head_sha(tmp_path):
    install_common = _load_install_common()
    target = tmp_path
    (target / ".ai" / "workflow").mkdir(parents=True)

    install_common.stamp_workflow_version(target, REPO_ROOT)

    version_file = target / ".ai" / "workflow" / ".version"
    assert version_file.exists(), "stamp must create .ai/workflow/.version"
    written = version_file.read_text(encoding="utf-8").strip()

    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert written == head, "stamp must record the template repo's HEAD sha"


def test_stamp_is_best_effort_without_git_repo(tmp_path):
    # script_dir is a plain (non-git) directory — stamping must not raise; it
    # simply skips so install/update under `set -euo pipefail` never aborts.
    install_common = _load_install_common()
    target = tmp_path / "target"
    (target / ".ai" / "workflow").mkdir(parents=True)
    non_git_src = tmp_path / "src"
    non_git_src.mkdir()

    install_common.stamp_workflow_version(target, non_git_src)  # must not raise

    assert not (target / ".ai" / "workflow" / ".version").exists()


# --- Fix 3: template repo ignores its own .version -----------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_template_repo_ignores_version_file():
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", ".ai/workflow/.version"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        ".ai/workflow/.version must be git-ignored in the template repo "
        "(it is a per-machine runtime artifact, never committed)"
    )
