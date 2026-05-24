"""Guard for the symlink-resolve fix in `_is_under_trusted_dir`.

Threat: a writable symlink in a trusted dir (e.g. `/usr/local/bin/bash`)
pointing at an attacker-controlled payload (`/home/x/evil`) used to pass
the prefix check because `os.path.normpath` does not follow symlinks.
The fix uses `os.path.realpath` so the prefix check sees the link's
target, not the link itself.

Static-lint pattern (no cross-platform symlink creation in CI).
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PTY_SESSION = REPO_ROOT / ".ai" / "dashboard" / "pty_session.py"


def _load_pty_session():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pty_session_for_test", PTY_SESSION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pty_session_for_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_is_under_trusted_dir_uses_realpath():
    """`_is_under_trusted_dir` body MUST call `os.path.realpath` so
    symlinks/junctions are resolved before the prefix check. Otherwise
    a symlink in a trusted dir pointing at attacker code passes.
    """
    mod = _load_pty_session()
    src = inspect.getsource(mod._is_under_trusted_dir)
    assert re.search(r"os\.path\.realpath\s*\(", src), (
        "_is_under_trusted_dir must call os.path.realpath to follow "
        "symlinks before checking the trusted-dir prefix list. The "
        "previous implementation used os.path.normpath which does not "
        "resolve links — a symlink in /usr/local/bin (or C:\\Windows\\"
        "System32) pointing at attacker code would pass the check."
    )


def test_is_under_trusted_dir_falls_back_to_normpath():
    """Broken / transient-FS-error paths must not return True or raise;
    fallback to normpath gives a deterministic answer.
    """
    mod = _load_pty_session()
    src = inspect.getsource(mod._is_under_trusted_dir)
    assert re.search(r"os\.path\.normpath\s*\(", src), (
        "Fallback to os.path.normpath must remain in place for the "
        "case where os.path.realpath raises OSError (broken link, "
        "permission denied, transient FS error)."
    )


def test_is_under_trusted_dir_rejects_unknown_paths():
    """Sanity: nonsense and obviously-attacker paths return False."""
    mod = _load_pty_session()
    assert mod._is_under_trusted_dir("") is False
    assert mod._is_under_trusted_dir("/home/attacker/evil") is False
    assert mod._is_under_trusted_dir("C:\\Users\\victim\\AppData\\evil.exe") is False
