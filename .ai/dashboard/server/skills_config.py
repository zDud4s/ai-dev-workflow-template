"""Filesystem scanners for skill + agent definition trees.

Extracted from serve.py. Both return one frontmatter-parsed record per
definition file -- ``_scan_agents_dir`` for ``<dir>/<name>.md`` agent files
(optionally recursing plugin trees) and ``_scan_skills_dir`` for
``<dir>/<name>/SKILL.md`` skill directories. Each tolerates missing dirs and
unreadable files (returns ``[]`` rather than raising) so callers can compose
results across multiple roots. Pure: only ROOT (for repo-relative paths) and
stdlib ``re``. serve.py re-exports both via a shim.
"""
from __future__ import annotations

import re
from pathlib import Path

from server.paths import ROOT


def _scan_agents_dir(agents_dir: Path, *, recursive: bool = False) -> list[dict]:
    """Return one record per ``<agents_dir>/<name>.md`` (or recursively,
    when ``recursive=True``, for plugin trees nested as
    ``.../agents/<name>.md``). Each record carries frontmatter fields
    ``name``, ``description``, ``tools``, ``model`` plus a repo-relative
    path.

    Tolerates missing dirs and unreadable files — returns ``[]`` rather
    than raising so callers can compose results across multiple roots.
    Agent files are single ``.md`` files (unlike skills which are
    directories with a ``SKILL.md`` inside)."""
    out: list[dict] = []
    try:
        if not agents_dir.is_dir():
            return out
        if recursive:
            files = sorted(agents_dir.glob("**/agents/*.md"))
        else:
            files = sorted(p for p in agents_dir.iterdir() if p.suffix == ".md")
    except OSError:
        return out
    for fp in files:
        try:
            if not fp.is_file():
                continue
        except OSError:
            continue
        name = fp.stem
        desc = ""
        tools = ""
        model = ""
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for ln in lines[1:]:
                if ln.strip() == "---":
                    break
                m = re.match(r"^(name|description|tools|model|color)\s*:\s*(.+)$", ln)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"\'')
                    if key == "name":
                        name = val
                    elif key == "description":
                        desc = val
                    elif key == "tools":
                        tools = val
                    elif key == "model":
                        model = val
        try:
            rel = str(fp.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(fp).replace("\\", "/")
        out.append({
            "name": name,
            "description": desc,
            "tools": tools,
            "model": model,
            "path": rel,
        })
    return out


def _scan_skills_dir(skills_dir: Path) -> list[dict]:
    """Return one record per ``<skills_dir>/<name>/SKILL.md`` containing
    the frontmatter ``name`` + ``description`` and a repo-relative path
    (or absolute path if the dir lives outside the repo).

    Tolerates missing dirs and unreadable files — returns ``[]`` rather
    than raising so callers can compose results across multiple roots."""
    out: list[dict] = []
    try:
        if not skills_dir.is_dir():
            return out
        subs = sorted(skills_dir.iterdir())
    except OSError:
        return out
    for sub in subs:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        name = sub.name
        desc = ""
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Minimal frontmatter parse: lines between leading ``---``.
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for ln in lines[1:]:
                if ln.strip() == "---":
                    break
                m = re.match(r"^(name|description)\s*:\s*(.+)$", ln)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"\'')
                    if key == "name":
                        name = val
                    elif key == "description":
                        desc = val
        try:
            rel = str(skill_md.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel = str(skill_md).replace("\\", "/")
        out.append({"name": name, "description": desc, "path": rel})
    return out
