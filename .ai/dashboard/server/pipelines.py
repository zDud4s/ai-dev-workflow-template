"""List saved agent-pipeline definitions under PIPELINES_DIR.

Extracted from serve.py. ``_list_pipelines`` scans ``.ai/local/pipelines/*.yaml``
(per-developer, gitignored DAG definitions authored by orchestrate-agents) and
returns one summary record each (slug, name, node/edge counts), tolerating
missing dir / malformed YAML. PyYAML is imported lazily. serve.py re-exports it
via a shim; the pipeline read/write endpoints stay in the Handler.
"""
from __future__ import annotations

from server.paths import PIPELINES_DIR, ROOT


def _list_pipelines() -> list[dict]:
    """List pipeline files for the dashboard. Excludes .gitkeep. Newest mtime first."""
    import yaml  # local import — PyYAML is only needed by this helper
    if not PIPELINES_DIR.is_dir():
        return []
    rows: list[dict] = []
    for p in PIPELINES_DIR.glob("*.yaml"):
        try:
            text = p.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text) or {}
        except (OSError, yaml.YAMLError):
            continue
        nodes = parsed.get("nodes") or []
        sink_kinds = ("synthesize", "collect", "passthrough")
        sink_kind = next(
            (n.get("kind") for n in nodes
             if isinstance(n, dict) and n.get("kind") in sink_kinds),
            "",
        )
        agent_count = sum(
            1 for n in nodes if isinstance(n, dict) and n.get("agent")
        )
        try:
            rel_path = str(p.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel_path = str(p).replace("\\", "/")
        rows.append({
            "slug": p.stem,
            "path": rel_path,
            "description": parsed.get("description") or "",
            "node_count": agent_count,
            "output_mode": sink_kind,
            "mtime": p.stat().st_mtime,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows
