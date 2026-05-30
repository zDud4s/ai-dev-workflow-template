from __future__ import annotations

import json
import os
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest
import serve


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def agent_runs_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(serve, "AGENT_RUNS_DIR", tmp_path)
    monkeypatch.setattr(serve, "METRICS_FILE", tmp_path / "metrics.jsonl")
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    return tmp_path


@pytest.fixture
def running_server(monkeypatch) -> Iterator[str]:
    httpd = _ThreadingTCPServer(("127.0.0.1", 0), serve.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(serve, "PORT", port)
    monkeypatch.setattr(serve, "BOUND_PORT", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _origin(base: str) -> str:
    parsed = urllib.parse.urlparse(base)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_json(base: str, path: str, *, origin: str | None = None) -> tuple[int, dict]:
    headers = {"Origin": _origin(base) if origin is None else origin}
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return exc.code, body


def _packet(dag_rows: list[tuple[str, str, str, str, str]] | None = None) -> str:
    rows = dag_rows or [
        ("plan", "agent-planner", "none", "Plan packet", "success"),
    ]
    table = "\n".join(
        f"| {node_id} | {agent} | {depends_on} | {expected_output} | {status} |"
        for node_id, agent, depends_on, expected_output, status in rows
    )
    return (
        "# Agent Dispatch Packet\n\n"
        "Task ID: endpoint-test\n"
        "Objective: Exercise the agent orchestration endpoint\n\n"
        "## Agent catalog\n"
        "- agent-planner\n\n"
        "## Subtask DAG\n"
        "| id | agent | depends_on | expected output | status |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"{table}\n\n"
        "## Output hint\n"
        "synthesize\n\n"
        "## Handoff\n\n"
        "Synthesis completed at: 2026-05-29T12:00:00Z\n"
        "Failed subtasks: none\n"
    )


def _write_run(
    directory: Path,
    slug: str,
    *,
    date: str = "2026-05-29",
    dag_rows: list[tuple[str, str, str, str, str]] | None = None,
) -> Path:
    path = directory / f"{date}-{slug}.md"
    path.write_text(_packet(dag_rows), encoding="utf-8", newline="\n")
    return path


def test_list_empty_returns_empty_runs(agent_runs_dir, running_server):
    status, body = _get_json(running_server, "/api/agent-orchestrations")

    assert status == 200
    assert body == {"runs": []}


def test_list_returns_runs_newest_first(agent_runs_dir, running_server):
    older = _write_run(agent_runs_dir, "older")
    newer = _write_run(agent_runs_dir, "newer")
    now = time.time()
    os.utime(older, (now - 60, now - 60))
    os.utime(newer, (now, now))

    status, body = _get_json(running_server, "/api/agent-orchestrations")

    assert status == 200
    assert [run["task_slug"] for run in body["runs"]] == ["newer", "older"]


def test_list_shape_fields(agent_runs_dir, running_server):
    _write_run(agent_runs_dir, "shape")

    status, body = _get_json(running_server, "/api/agent-orchestrations")

    assert status == 200
    assert {
        "task_slug",
        "date",
        "plan_ts",
        "dispatch_count",
        "synthesis_ts",
        "success",
        "path",
    }.issubset(body["runs"][0])


def test_detail_returns_parsed_dag(agent_runs_dir, running_server):
    _write_run(agent_runs_dir, "dag-run", dag_rows=[
        ("a", "agent-planner", "none", "Plan branches", "success"),
        ("b", "frontend-builder", "a", "Build UI", "success"),
        ("c", "reviewer", "a,b", "Review output", "pending"),
    ])

    status, body = _get_json(running_server, "/api/agent-orchestrations/dag-run")

    assert status == 200
    assert body["dag"] == [
        {
            "id": "a",
            "agent": "agent-planner",
            "status": "success",
            "expected_output": "Plan branches",
            "depends_on": [],
        },
        {
            "id": "b",
            "agent": "frontend-builder",
            "status": "success",
            "expected_output": "Build UI",
            "depends_on": ["a"],
        },
        {
            "id": "c",
            "agent": "reviewer",
            "status": "pending",
            "expected_output": "Review output",
            "depends_on": ["a", "b"],
        },
    ]


def test_detail_includes_metrics_cross_ref(agent_runs_dir, running_server):
    _write_run(agent_runs_dir, "metrics-run")
    (agent_runs_dir / "metrics.jsonl").write_text(
        json.dumps({"task_slug": "metrics-run", "phase": "agent_plan"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    status, body = _get_json(running_server, "/api/agent-orchestrations/metrics-run")

    assert status == 200
    assert body["metrics"] == [{"task_slug": "metrics-run", "phase": "agent_plan"}]


def test_detail_path_traversal_rejected(agent_runs_dir, running_server):
    slugs = [
        "..%2F..%2Fetc%2Fpasswd",
        "..%5C..%5Cetc",
        "%2e%2e%2fpasswd",
        "%2Fetc%2Fpasswd",
    ]

    for slug in slugs:
        status, _body = _get_json(running_server, f"/api/agent-orchestrations/{slug}")
        assert status != 200
        assert status in {400, 404}


def test_detail_unknown_slug_returns_404(agent_runs_dir, running_server):
    status, body = _get_json(running_server, "/api/agent-orchestrations/missing-run")

    assert status == 404
    assert body["error"] == "agent orchestration not found"


def test_list_origin_gate_blocks_cross_origin(agent_runs_dir, running_server):
    status, body = _get_json(
        running_server,
        "/api/agent-orchestrations",
        origin="http://evil.example",
    )

    assert status == 403
    assert body["error"] == "origin not allowed"


def test_detail_origin_gate_blocks_cross_origin(agent_runs_dir, running_server):
    status, body = _get_json(
        running_server,
        "/api/agent-orchestrations/anything",
        origin="http://evil.example",
    )

    assert status == 403
    assert body["error"] == "origin not allowed"
