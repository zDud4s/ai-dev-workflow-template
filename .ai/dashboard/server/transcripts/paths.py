from __future__ import annotations

import json
import time
from pathlib import Path

# Memo of resolved ~/.claude/projects/<slug> dir, keyed by (str(cwd),
# str(projects_root)) so a changed projects-root override never returns a dir
# resolved against a stale root. Positive entries do not expire; negative
# entries expire quickly because Claude may create the project dir just after
# the first lookup.
_TRANSCRIPTS_DIR_NEG_TTL_S = 3.0
_TRANSCRIPTS_DIR_CACHE: dict[tuple[str, str | None], tuple[Path | None, float]] = {}

# IDE transcript mirror: Claude Code (the VSCode/Cursor extension) writes a
# JSONL transcript of every session to ~/.claude/projects/<encoded-cwd>/<sid>.jsonl
# so the dashboard can tail those files and surface ANY ongoing IDE chat as
# a read-only terminal pane. Tests override this with a tmp tree.
_CLAUDE_PROJECTS_ROOT_OVERRIDE: Path | None = None


def _claude_projects_root() -> Path | None:
    if _CLAUDE_PROJECTS_ROOT_OVERRIDE is not None:
        return _CLAUDE_PROJECTS_ROOT_OVERRIDE
    home = Path.home()
    candidate = home / ".claude" / "projects"
    return candidate if candidate.is_dir() else None


def _transcripts_dir_for_cwd(cwd: Path) -> Path | None:
    """Pick the ``~/.claude/projects/<slug>`` directory matching ``cwd``.

    Claude Code's slug rule (observed): replace ``:``, ``/``, ``\\``, ``.``
    and spaces with ``-``. We try a few common variants because case-folding
    of the drive letter has been seen both ways across machines."""
    root = _claude_projects_root()
    # Key the memo by BOTH the cwd and the current projects root. The root is
    # an overridable module global (tests monkeypatch ``_CLAUDE_PROJECTS_ROOT_
    # OVERRIDE`` to point at a tmp tree); keying on cwd alone would hand back a
    # stale dir resolved against a previous root once that override changes.
    key = (str(cwd), str(root) if root is not None else None)
    now = time.monotonic()
    cached = _TRANSCRIPTS_DIR_CACHE.get(key)
    if cached is not None:
        cached_path, cached_ts = cached
        if cached_path is not None:
            return cached_path
        if now - cached_ts < _TRANSCRIPTS_DIR_NEG_TTL_S:
            return None
    if root is None:
        _TRANSCRIPTS_DIR_CACHE[key] = (None, time.monotonic())
        return None
    s = str(cwd)
    slug_lower = (s[0].lower() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-").replace(".", "-")
    slug_upper = (s[0].upper() + s[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-").replace(".", "-")
    for slug in (slug_lower, slug_upper, slug_lower.lower()):
        p = root / slug
        if p.is_dir():
            _TRANSCRIPTS_DIR_CACHE[key] = (p, time.monotonic())
            return p
    # Last-ditch: scan all subdirs and check if any transcript records this cwd.
    target = str(cwd).lower()
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        # Peek the first non-empty line of any jsonl file looking for a cwd match.
        for f in sub.glob("*.jsonl"):
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if str(obj.get("cwd") or "").lower() == target:
                            _TRANSCRIPTS_DIR_CACHE[key] = (sub, time.monotonic())
                            return sub
                        break  # only peek first record per file
            except OSError:
                continue
    _TRANSCRIPTS_DIR_CACHE[key] = (None, time.monotonic())
    return None


# Codex stores per-session rollouts here. Tests override via
# ``_CODEX_SESSIONS_ROOT_OVERRIDE``.
_CODEX_SESSIONS_ROOT_OVERRIDE: Path | None = None


def _codex_sessions_root() -> Path | None:
    if _CODEX_SESSIONS_ROOT_OVERRIDE is not None:
        return _CODEX_SESSIONS_ROOT_OVERRIDE
    p = Path.home() / ".codex" / "sessions"
    return p if p.is_dir() else None
