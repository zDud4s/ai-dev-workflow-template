"""Agent-run (.md) parsing for the dashboard server.

Extracted from serve.py. These helpers read the filled agent-dispatch packets
under ``AGENT_RUNS_DIR`` (Objective / Output hint / Subtask DAG / Handoff
sections) and project them into the dicts the dashboard renders, joining each
run to its metrics rows by ``task_slug``. The module owns its own mtime-keyed
parse cache. serve.py re-exports every name here, so ``serve._parse_agent_run``
/ ``serve._list_agent_runs`` and the tests that reference them keep working.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import threading
from pathlib import Path

from server.storage import _bound_path_cache, _load_jsonl_cached
from server.validation import _is_under_trusted_dir
from server.paths import AGENT_RUNS_DIR, METRICS_FILE, ROOT

# mtime-keyed cache of parsed agent-run .md files: (str(path), st.st_mtime_ns) -> parsed dict.
_AGENT_RUN_PARSE_CACHE: dict[str, tuple[int, dict]] = {}
_AGENT_RUN_PARSE_LOCK = threading.Lock()


def _agent_run_slug_date(path: Path) -> tuple[str, str | None]:
    stem = path.stem
    match = re.fullmatch(r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<slug>.+)", stem)
    if match:
        slug = match.group("slug")
        date = match.group("date")
    else:
        slug = stem
        date = None
    slug = re.sub(r"-\d+$", "", slug)
    return slug or stem, date


def _markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"(?im)^##\s+{re.escape(heading)}\s*$")
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"(?m)^##\s+", text[match.end():])
    end = match.end() + next_match.start() if next_match else len(text)
    return text[match.end():end].strip()


def _first_section_value(section: str) -> str | None:
    for line in section.splitlines():
        value = line.strip()
        if not value or value.startswith("<!--"):
            continue
        if value.startswith("- "):
            value = value[2:].strip()
        return value or None
    return None


def _line_value(text: str, label: str) -> str | None:
    pattern = re.compile(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.*?)\s*$")
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.startswith("<!--"):
        return None
    return value


def _normalise_agent_run_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _strip_agent_run_value(value: str) -> str:
    value = value.strip().strip(",")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _parse_agent_run_depends_on(value: str | None) -> list[str]:
    if not value:
        return []
    raw = _strip_agent_run_value(value)
    if not raw or raw.lower() in {"none", "null", "n/a", "na", "-", "[]"}:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    parts = re.split(r"\s*,\s*|\s+", raw)
    out: list[str] = []
    for part in parts:
        item = _strip_agent_run_value(part.strip().strip("[]"))
        if item and item.lower() not in {"none", "null", "n/a", "na", "-"}:
            out.append(item)
    return out


def _agent_run_node(fields: dict[str, str]) -> dict:
    def pick(*keys: str) -> str | None:
        for key in keys:
            value = fields.get(key)
            if value:
                return value
        return None

    status = pick("status") or "pending"
    return {
        "id": pick("id", "task_id", "subtask_id"),
        "agent": pick("agent", "subagent", "subagent_type"),
        "status": status,
        "expected_output": pick("expected_output", "expected", "output"),
        "depends_on": _parse_agent_run_depends_on(pick("depends_on", "depends")),
    }


def _split_markdown_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _parse_agent_run_dag_table(section: str) -> list[dict]:
    lines = [line for line in section.splitlines() if line.strip()]
    for idx, line in enumerate(lines[:-1]):
        if "|" not in line or not _is_markdown_table_separator(lines[idx + 1]):
            continue
        headers = [_normalise_agent_run_key(cell) for cell in _split_markdown_table_row(line)]
        nodes: list[dict] = []
        for row in lines[idx + 2:]:
            if "|" not in row:
                break
            cells = _split_markdown_table_row(row)
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            fields = {
                key: _strip_agent_run_value(value)
                for key, value in zip(headers, cells)
                if key
            }
            if not any(fields.values()):
                continue
            nodes.append(_agent_run_node(fields))
        if nodes:
            return nodes
    return []


def _parse_agent_run_inline_fields(value: str) -> dict[str, str]:
    raw = value.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    fields: dict[str, str] = {}
    for item in re.split(r",\s*(?=[A-Za-z_][A-Za-z0-9 _-]*\s*:)", raw):
        if ":" not in item:
            continue
        key, val = item.split(":", 1)
        fields[_normalise_agent_run_key(key)] = _strip_agent_run_value(val)
    return fields


def _parse_agent_run_dag_yamlish(section: str) -> list[dict]:
    nodes: list[dict] = []
    current: dict[str, str] | None = None
    last_key: str | None = None

    def flush() -> None:
        nonlocal current, last_key
        if current and any(current.values()):
            nodes.append(_agent_run_node(current))
        current = None
        last_key = None

    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.startswith("- "):
            rest = stripped[2:].strip()
            if current is not None and last_key and ":" not in rest:
                existing = current.get(last_key, "")
                current[last_key] = f"{existing}, {rest}" if existing else rest
                continue
            flush()
            current = {}
            if rest:
                current.update(_parse_agent_run_inline_fields(rest))
                last_key = next(reversed(current), None) if current else None
            continue
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        last_key = _normalise_agent_run_key(key)
        current[last_key] = _strip_agent_run_value(value)
    flush()
    return nodes


def _parse_agent_run_dag(section: str, path: Path) -> list[dict]:
    dag = _parse_agent_run_dag_table(section)
    if dag:
        return dag
    dag = _parse_agent_run_dag_yamlish(section)
    if dag:
        return dag
    meaningful = [
        line.strip() for line in section.splitlines()
        if line.strip() and not line.strip().startswith("<!--")
    ]
    if meaningful:
        print(f"[serve] agent-run DAG parse failed for {path}", flush=True)
    return []


def _extract_handoff_synthesis_ts(handoff: str) -> str | None:
    timestamp = r"\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:\d{2})?"
    pattern = re.compile(
        rf"(?im)^\s*(?:synthesis[_ -]?ts|synthesis timestamp|"
        rf"synthesis completed(?: at)?|completed_at)\s*:\s*({timestamp})\s*$"
    )
    match = pattern.search(handoff)
    return match.group(1) if match else None


def _extract_handoff_field(handoff: str, label: str) -> str | None:
    labels = (
        "Synthesis output",
        "Per-subtask results",
        "Failed subtasks",
        "Memory updates",
        "Phase execution log",
    )
    next_label = "|".join(re.escape(item) for item in labels if item != label)
    pattern = re.compile(
        rf"(?ims)^\s*{re.escape(label)}\s*:\s*(.*?)"
        rf"(?=^\s*(?:{next_label})\s*:|^##\s+|\Z)"
    )
    match = pattern.search(handoff)
    if not match:
        return None
    return match.group(1).strip()


def _extract_agent_run_success(handoff: str) -> bool | None:
    explicit = re.search(r"(?im)^\s*(?:success|succeeded)\s*:\s*(true|false|yes|no|1|0)\s*$", handoff)
    if explicit:
        return explicit.group(1).lower() in {"true", "yes", "1"}
    status = re.search(r"(?im)^\s*status\s*:\s*(success|succeeded|done|failed|error)\s*$", handoff)
    if status:
        return status.group(1).lower() in {"success", "succeeded", "done"}
    failed = _extract_handoff_field(handoff, "Failed subtasks")
    if failed is None:
        return None
    cleaned = re.sub(r"(?m)^\s*[-*]\s*", "", failed).strip().lower()
    if cleaned in {"none", "n/a", "na", "null", "[]", "-"}:
        return True
    if cleaned:
        return False
    return None


def _parse_agent_run(path: Path) -> dict:
    task_slug, date = _agent_run_slug_date(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[serve] agent-run read failed for {path}: {e}", flush=True)
        return {
            "task_slug": task_slug,
            "date": date,
            "objective": None,
            "output_hint": None,
            "dag": [],
            "handoff": "",
        }
    objective = _first_section_value(_markdown_section(text, "Objective")) or _line_value(text, "Objective")
    output_hint = _first_section_value(_markdown_section(text, "Output hint")) or _line_value(text, "Output hint")
    dag_section = _markdown_section(text, "Subtask DAG")
    handoff = _markdown_section(text, "Handoff")
    return {
        "task_slug": task_slug,
        "date": date,
        "objective": objective,
        "output_hint": output_hint,
        "dag": _parse_agent_run_dag(dag_section, path) if dag_section else [],
        "handoff": handoff,
    }


def _agent_run_metrics_by_slug() -> dict[str, list[dict]]:
    by_slug: dict[str, list[dict]] = {}
    for row in _load_jsonl_cached(METRICS_FILE):
        if not isinstance(row, dict):
            continue
        slug = row.get("task_slug")
        if isinstance(slug, str) and slug:
            by_slug.setdefault(slug, []).append(row)
    return by_slug


def _list_agent_runs() -> list[dict]:
    if not AGENT_RUNS_DIR.is_dir():
        return []
    metrics_by_slug = _agent_run_metrics_by_slug()
    trusted_root = os.path.realpath(str(AGENT_RUNS_DIR))
    runs: list[dict] = []
    for path in AGENT_RUNS_DIR.glob("*.md"):
        if path.name == ".gitkeep":
            continue
        try:
            resolved = path.resolve(strict=True)
            if not _is_under_trusted_dir(resolved, trusted_root):
                print(f"[serve] agent-run outside trusted dir skipped: {path}", flush=True)
                continue
            st = path.stat()
        except OSError as e:
            print(f"[serve] agent-run stat failed for {path}: {e}", flush=True)
            continue
        parse_key = str(path)
        mtime_ns = st.st_mtime_ns
        with _AGENT_RUN_PARSE_LOCK:
            cached = _AGENT_RUN_PARSE_CACHE.get(parse_key)
            if cached is not None and cached[0] == mtime_ns:
                parsed = cached[1]
            else:
                parsed = _parse_agent_run(path)
                _AGENT_RUN_PARSE_CACHE[parse_key] = (mtime_ns, parsed)
                _bound_path_cache(_AGENT_RUN_PARSE_CACHE)
        slug = parsed.get("task_slug")
        handoff = parsed.get("handoff") or ""
        plan_ts = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).isoformat(timespec="seconds")
        try:
            rel_path = str(path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel_path = str(path)
        runs.append({
            "task_slug": slug,
            "date": parsed.get("date"),
            "plan_ts": plan_ts.replace("+00:00", "Z"),
            "dispatch_count": len(parsed.get("dag") or []),
            "synthesis_ts": _extract_handoff_synthesis_ts(handoff),
            "success": _extract_agent_run_success(handoff),
            "path": rel_path,
            "metrics": metrics_by_slug.get(slug, []) if isinstance(slug, str) else [],
        })
    runs.sort(key=lambda row: row.get("plan_ts") or "", reverse=True)
    return runs
