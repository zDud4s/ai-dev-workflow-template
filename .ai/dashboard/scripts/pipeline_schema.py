"""Pure-Python validator for pipeline YAML.

Used by:
- run-pipeline skill (Pre-flight load-time validation)
- dashboard PUT /api/pipelines/<slug> endpoint

The function accepts an already-parsed Python dict (the caller is responsible
for yaml.safe_load); returns (ok: bool, errors: list[str]). All errors share
the prefix `pipeline invalid:` for consistent surfacing.
"""
from __future__ import annotations
from typing import Any

VALID_OUTPUT_MODES = ("synthesize", "passthrough", "per-agent")


def is_linear(pipeline: dict[str, Any]) -> bool:
    """A pipeline with no `depends_on` field on any node is linear; the
    orchestrator interprets the node order as the dependency chain."""
    nodes = pipeline.get("nodes") or []
    return all("depends_on" not in n for n in nodes)


def validate(pipeline: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate the parsed-YAML shape of a pipeline against the schema."""
    errs: list[str] = []

    # Required top-level keys
    if "output" not in pipeline:
        errs.append("pipeline invalid: missing key 'output'")
    if "nodes" not in pipeline:
        errs.append("pipeline invalid: missing key 'nodes'")

    nodes = pipeline.get("nodes")
    if nodes is not None and (not isinstance(nodes, list) or not nodes):
        errs.append("pipeline invalid: nodes must be a non-empty list")
        nodes = []
    elif nodes is None:
        nodes = []

    # Per-node shape + uniqueness
    seen: set[str] = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            errs.append(f"pipeline invalid: node #{i} must be a mapping")
            continue
        node_id = node.get("id")
        node_agent = node.get("agent")
        if not isinstance(node_id, str) or not node_id:
            errs.append(f"pipeline invalid: node #{i} missing id or agent")
            continue
        if not isinstance(node_agent, str) or not node_agent:
            errs.append(f"pipeline invalid: node #{i} missing id or agent")
        if node_id in seen:
            errs.append(f"pipeline invalid: duplicate id '{node_id}'")
        seen.add(node_id)

    # depends_on resolution
    ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        deps = node.get("depends_on")
        if deps is None:
            continue
        if not isinstance(deps, list):
            errs.append(f"pipeline invalid: depends_on for '{node.get('id')}' must be a list")
            continue
        for d in deps:
            if d not in ids:
                errs.append(
                    f"pipeline invalid: '{node.get('id')}' depends on unknown '{d}'"
                )

    # Cycle check via Kahn's algorithm
    if not errs:
        in_deg = {n["id"]: len(n.get("depends_on") or []) for n in nodes}
        # If pipeline is linear (no depends_on anywhere), no cycle possible.
        if any("depends_on" in n for n in nodes):
            children: dict[str, list[str]] = {n["id"]: [] for n in nodes}
            for n in nodes:
                for d in n.get("depends_on") or []:
                    children[d].append(n["id"])
            ready = [nid for nid, deg in in_deg.items() if deg == 0]
            visited = 0
            while ready:
                nid = ready.pop()
                visited += 1
                for child in children[nid]:
                    in_deg[child] -= 1
                    if in_deg[child] == 0:
                        ready.append(child)
            if visited != len(nodes):
                errs.append("pipeline invalid: cycle detected")

    # output mode + passthrough node coherence
    out = pipeline.get("output") or {}
    if isinstance(out, dict):
        mode = out.get("mode")
        if mode not in VALID_OUTPUT_MODES:
            errs.append(
                "pipeline invalid: output.mode must be synthesize/passthrough/per-agent"
            )
        elif mode == "passthrough":
            target = out.get("node")
            if not target or target not in ids:
                errs.append(
                    f"pipeline invalid: passthrough node '{target}' not in nodes"
                )

    return (not errs, errs)
