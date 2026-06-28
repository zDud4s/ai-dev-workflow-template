"""Tiny dependency-free config readers for the dashboard server.

``_read_yaml_field`` pulls a single top-level mapping (e.g. ``session: {...}``)
out of ``.ai/models.yaml`` without a PyYAML dependency. Shared here because both
the jobs and improver domains read model-config blocks at startup; keeping it in
a neutral module lets those domains import it without a serve.py back-import.
"""
from __future__ import annotations

import re
from pathlib import Path


def _read_yaml_field(path: Path, field: str) -> dict:
    """Minimal YAML helper to pull a top-level mapping like `session: {...}`.

    Avoids a PyYAML dependency. Only handles the simple two-line shape used in
    .ai/models.yaml. Returns an empty dict on any failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if not in_block:
            if stripped.startswith(field + ":"):
                in_block = True
            continue
        if not stripped:
            break
        if not stripped.startswith((" ", "\t")):
            break
        m = re.match(r"^\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(\S.*)?$", stripped)
        if not m:
            continue
        val = (m.group(2) or "").strip()
        val = val.split("#", 1)[0].strip()  # strip inline comment
        out[m.group(1)] = val.strip('"\'')
    return out
