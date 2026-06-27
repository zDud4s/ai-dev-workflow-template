"""Read and edit the model catalog / role assignments in .ai/models.yaml.

Extracted from serve.py. ``_read_models_catalog`` parses the ``catalog:`` block
(the per-tool model list that drives the dashboard model pickers), falling back
to ``_MODELS_CATALOG_FALLBACK`` when the YAML is missing / unreadable / PyYAML
is absent. ``_patch_or_create_block`` / ``_patch_phase_block`` are the pure
text transforms behind the "edit models.yaml" endpoint (update a named block's
fields, or one phase's tool/model, in place — preserving comments and layout).

Pure apart from ROOT + stdlib (PyYAML is imported lazily inside
``_read_models_catalog``). serve.py re-exports every name via a shim.
"""
from __future__ import annotations

import re
from pathlib import Path

from server.paths import ROOT


# the live source of truth is always the YAML. Keep the shape identical to
# what _read_models_catalog returns: {tool: [model_id, ...]}, newest-first.
_MODELS_CATALOG_FALLBACK: dict[str, list[str]] = {
    "claude": ["claude-opus-4-8", "claude-fable-5", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "codex":  ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
}


def _read_models_catalog(path: Path | None = None) -> dict[str, list[str]]:
    """Read the ``catalog:`` block from .ai/models.yaml — the single source of
    truth for which models exist per tool.

    Returns ``{tool: [model_id, ...]}`` newest-first. Each catalog entry is a
    mapping ``{id: ..., ...}``; only the ``id`` is surfaced here (notes/labels
    stay in the YAML as inline comments). Falls back to
    ``_MODELS_CATALOG_FALLBACK`` on any failure (missing file, no PyYAML, no
    ``catalog`` block, malformed shape) so model pickers never render empty.
    """
    if path is None:
        path = ROOT / ".ai" / "models.yaml"
    try:
        import yaml  # local import — PyYAML only needed by this helper
        parsed = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        catalog = parsed.get("catalog")
        if not isinstance(catalog, dict):
            return {k: list(v) for k, v in _MODELS_CATALOG_FALLBACK.items()}
        out: dict[str, list[str]] = {}
        for tool, entries in catalog.items():
            if not isinstance(entries, list):
                continue
            ids: list[str] = []
            for entry in entries:
                if isinstance(entry, dict):
                    mid = entry.get("id")
                elif isinstance(entry, str):
                    mid = entry
                else:
                    mid = None
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
            if ids:
                out[str(tool)] = ids
        return out or {k: list(v) for k, v in _MODELS_CATALOG_FALLBACK.items()}
    except Exception:  # noqa: BLE001 — a bad config must never break model pickers
        return {k: list(v) for k, v in _MODELS_CATALOG_FALLBACK.items()}


def _patch_or_create_block(text: str, name: str, updates: dict[str, str | None],
                           creator_template: str = "") -> str:
    """Same as _patch_phase_block but appends a fresh block if the header is missing.

    creator_template is the initial YAML to insert (e.g. ``improver:\\n  enabled: true\\n``).
    """
    try:
        return _patch_phase_block(text, name, updates)
    except ValueError:
        if not creator_template:
            creator_template = f"{name}:\n"
        seed = text.rstrip("\n") + "\n\n" + creator_template
        if not seed.endswith("\n"):
            seed += "\n"
        return _patch_phase_block(seed, name, updates)


def _patch_phase_block(text: str, phase: str, updates: dict[str, str | None]) -> str:
    """Update fields under a top-level YAML mapping like ``plan:\\n  tool: ...``.

    For each key in updates:
      - value is a string -> replace existing `  <key>: <old>` line, or insert
        as the first child line after the header
      - value is None     -> remove the `  <key>: ...` line if present
    """
    lines = text.splitlines(keepends=False)
    n = len(lines)
    header_idx = None
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(phase)}\s*:\s*(#.*)?$", ln):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"phase block `{phase}:` not found in models.yaml")
    # Find end of this block (next non-indented, non-blank line)
    end_idx = n
    for j in range(header_idx + 1, n):
        ln = lines[j]
        if ln.strip() == "":
            continue
        if not ln.startswith((" ", "\t")):
            end_idx = j
            break
    block = lines[header_idx + 1 : end_idx]
    # Track existing keys
    key_re = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*)?$")
    indent = "  "
    for ln in block:
        m = key_re.match(ln)
        if m:
            indent = m.group(1)
            break

    def render(key: str, val: str) -> str:
        return f"{indent}{key}: {val}"

    new_block: list[str] = list(block)
    for key, val in updates.items():
        existing_idx = None
        for k, ln in enumerate(new_block):
            m = key_re.match(ln)
            if m and m.group(2) == key:
                existing_idx = k
                break
        if val is None:
            if existing_idx is not None:
                new_block.pop(existing_idx)
            continue
        if existing_idx is not None:
            # Preserve inline comment if any
            ln = new_block[existing_idx]
            m = re.match(r"^(\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*)\S+(\s*(?:#.*)?)$", ln)
            if m:
                new_block[existing_idx] = f"{m.group(1)}{val}{m.group(2)}"
            else:
                new_block[existing_idx] = render(key, val)
        else:
            # Insert as the last non-empty child line
            insert_at = len(new_block)
            while insert_at > 0 and new_block[insert_at - 1].strip() == "":
                insert_at -= 1
            new_block.insert(insert_at, render(key, val))

    new_lines = lines[: header_idx + 1] + new_block + lines[end_idx:]
    out = "\n".join(new_lines)
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out
