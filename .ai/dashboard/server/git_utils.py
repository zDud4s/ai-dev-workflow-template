"""TTL-cached ``git ls-files`` results, keyed by working directory.

Extracted from serve.py. ``_git_lsfiles_cached`` returns the cached tracked-file
list for a cwd when fresh (within ``_GIT_LSFILES_TTL_S``), else None so the
caller runs ``git ls-files`` and stores the result via ``_git_lsfiles_put``.
Owns the ``_GIT_LSFILES_*`` cache + lock. serve.py re-exports every name via a
shim.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_GIT_LSFILES_CACHE: dict[str, tuple[float, int, list[str]]] = {}
_GIT_LSFILES_LOCK = threading.Lock()
_GIT_LSFILES_TTL_S = 10.0


def _git_lsfiles_cached(cwd: Path) -> list[str] | None:
    try:
        st = (cwd / ".git" / "index").stat()
    except OSError:
        return None
    with _GIT_LSFILES_LOCK:
        entry = _GIT_LSFILES_CACHE.get(str(cwd))
        if entry is None:
            return None
        cached_at, index_mtime_ns, lines = entry
        if (time.monotonic() - cached_at) < _GIT_LSFILES_TTL_S and index_mtime_ns == st.st_mtime_ns:
            return lines
    return None


def _git_lsfiles_put(cwd: Path, lines: list[str]) -> None:
    try:
        st = (cwd / ".git" / "index").stat()
    except OSError:
        return
    with _GIT_LSFILES_LOCK:
        _GIT_LSFILES_CACHE[str(cwd)] = (time.monotonic(), st.st_mtime_ns, list(lines))
