"""Pure validation / path helpers for the dashboard server.

Extracted from ``serve.py`` — these functions hold no shared mutable state
and depend only on the stdlib, so they move cleanly. ``serve.py`` re-exports
every name here for backward compatibility (``serve._safe_which`` etc.).
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil

# Source of truth for the workflow template. /api/workflow/check and
# /api/workflow/update clone this fresh on each call so a one-click update from
# the dashboard always reflects the latest upstream version. Override via
# AI_WORKFLOW_TEMPLATE_URL (useful for forks or hosted test mirrors).
_DEFAULT_WORKFLOW_TEMPLATE_URL = "https://github.com/zDud4s/ai-dev-workflow-template.git"
# Allowlisted scheme + host pairs for AI_WORKFLOW_TEMPLATE_URL.
# https://github.com / https://gitlab.com / https://codeberg.org cover the
# common fork hosts; git+https keeps explicit Git transport URLs available.
# Anything else (file://, http://, git://, ssh://, http://attacker/) is rejected
# and the default is used so a tampered env var can't redirect every dashboard
# click to a hostile clone.
_ALLOWED_TEMPLATE_HOSTS = {
    ("https", "github.com"),
    ("https", "gitlab.com"),
    ("https", "codeberg.org"),
    ("git+https", "github.com"),
    ("git+https", "gitlab.com"),
    ("git+https", "codeberg.org"),
}


def _validate_template_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
    except (ValueError, TypeError):
        return _DEFAULT_WORKFLOW_TEMPLATE_URL
    if p.scheme == "file":
        print(
            f"[serve] AI_WORKFLOW_TEMPLATE_URL rejected (file:// scheme not allowed): {url!r}",
            flush=True,
        )
        return _DEFAULT_WORKFLOW_TEMPLATE_URL
    if (p.scheme, (p.hostname or "").lower()) in _ALLOWED_TEMPLATE_HOSTS:
        return url
    print(
        f"[serve] AI_WORKFLOW_TEMPLATE_URL rejected (scheme/host not allowlisted): {url!r}",
        flush=True,
    )
    return _DEFAULT_WORKFLOW_TEMPLATE_URL


def _safe_which(name: str) -> str | None:
    """Hardened wrapper around ``shutil.which``.

    Drops obviously hostile / accidental PATH entries (empty, ``.``,
    relative, $TEMP, $HOME/Downloads) BEFORE the lookup so a planted
    binary in those locations can't shadow the real tool. Returns the
    absolute resolved path, or ``None`` if no acceptable match was
    found. Falls back to ``None`` even when the unfiltered ``which``
    would have matched — callers MUST handle ``None``.
    """
    raw_path = os.environ.get("PATH", "")
    if not raw_path:
        return None
    sep = os.pathsep
    bad_dirs: set[str] = set()
    for envvar in ("TEMP", "TMP", "TMPDIR"):
        val = os.environ.get(envvar)
        if val:
            try:
                bad_dirs.add(os.path.normcase(os.path.realpath(val)))
            except OSError:
                bad_dirs.add(os.path.normcase(val))
    home = os.path.expanduser("~")
    if home and home != "~":
        for sub in ("Downloads", "Desktop"):
            cand = os.path.join(home, sub)
            try:
                bad_dirs.add(os.path.normcase(os.path.realpath(cand)))
            except OSError:
                bad_dirs.add(os.path.normcase(cand))
    cleaned: list[str] = []
    for entry in raw_path.split(sep):
        if not entry or entry in (".", ".."):
            continue
        if not os.path.isabs(entry):
            continue
        try:
            resolved = os.path.normcase(os.path.realpath(entry))
        except OSError:
            continue
        if resolved in bad_dirs:
            continue
        cleaned.append(entry)
    if not cleaned:
        return None
    return shutil.which(name, path=sep.join(cleaned))


def _is_under_trusted_dir(path, trusted_dir) -> bool:
    """Return True when ``path`` resolves inside ``trusted_dir``."""
    try:
        path_real = os.path.normcase(os.path.realpath(str(path)))
        trusted_real = os.path.normcase(os.path.realpath(str(trusted_dir)))
        return os.path.commonpath([path_real, trusted_real]) == trusted_real
    except (OSError, ValueError):
        return False


def _parse_iso_ts(s):
    """Return a timezone-aware UTC datetime, or None on failure."""
    if not isinstance(s, str) or not s:
        return None
    raw = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _normalise_path_for_match(s: str) -> str:
    return (s or "").lower().replace("\\", "/").rstrip("/")


def _iso_to_epoch(s: str) -> float:
    """Lossy ISO-8601 -> epoch seconds; returns 0 on parse failure."""
    if not s:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _skill_name_canonical(raw: str) -> str:
    """Strip plugin namespace prefix from a skill id (``a:b:c`` -> ``c``)."""
    if not raw:
        return ""
    return raw.rsplit(":", 1)[-1].strip()
