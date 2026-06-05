"""Tests for SessionRegistry integration in serve.py (Task 5).

Verifies that SESSION_REGISTRY is exposed at module level and that
_session_engine_factory spawns a ``claude --resume <sid>`` subprocess
backed by _start_subprocess_job / _build_chat_argv.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.client import HTTPResponse
from pathlib import Path
from urllib.parse import urlparse

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


@pytest.fixture(scope="module")
def serve_module():
    """Load `.ai/dashboard/serve.py` as a module without running main()."""
    spec = importlib.util.spec_from_file_location("dashboard_serve", SERVE_PATH)
    assert spec and spec.loader, "could not load serve.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_jobs_dir(tmp_path, monkeypatch, serve_module):
    """Redirect ``serve_module.JOBS_DIR`` to a per-test tmp directory."""
    monkeypatch.setattr(serve_module, "JOBS_DIR", tmp_path / "jobs")


@pytest.fixture
def running_server(serve_module):
    """Start the dashboard HTTP server on an ephemeral port in a thread."""
    import socketserver

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), serve_module.Handler)
    port = httpd.server_address[1]
    original_port = serve_module.PORT
    original_bound = serve_module.BOUND_PORT
    serve_module.PORT = port
    serve_module.BOUND_PORT = port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        serve_module.PORT = original_port
        serve_module.BOUND_PORT = original_bound
        httpd.shutdown()
        httpd.server_close()


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes, dict]:
    # Auto-inject a same-origin Origin header on mutating requests so the
    # CSRF guard accepts them.
    merged = dict(headers or {})
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and "Origin" not in merged:
        parsed = urlparse(url)
        merged["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    req = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # type: HTTPResponse
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


@pytest.fixture(autouse=True)
def _reset_session_registry(serve_module):
    serve_module.SESSION_REGISTRY._sessions.clear()
    yield
    serve_module.SESSION_REGISTRY._sessions.clear()


def test_engine_factory_builds_resume_argv(serve_module, monkeypatch):
    captured = {}
    real_popen = serve_module.subprocess.Popen
    def fake_popen(argv, **kw):
        captured["argv"] = list(argv)
        return real_popen([serve_module.sys.executable, "-c", "pass"], **kw)
    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)

    assert hasattr(serve_module, "SESSION_REGISTRY")
    eng = serve_module._session_engine_factory("sid-xyz", "claude-sonnet-4-6")
    # The engine starts the resume job at CONSTRUCTION time (via _start_subprocess_job),
    # so argv is captured here already; submit() only feeds stdin.
    for _ in range(40):
        if "argv" in captured: break
        serve_module.time.sleep(0.05)
    assert "--resume" in captured["argv"] and "sid-xyz" in captured["argv"]
    eng.submit({"text": "oi"})


def test_sessions_list_merges_ide_transcripts_and_dashboard(running_server, serve_module, tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects"
    slug = str(serve_module.ROOT).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    (projects / slug).mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0001"
    (projects / slug / f"{sid}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"oi"}}\n', encoding="utf-8")
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    status, body, _ = _http("GET", f"{running_server}/api/sessions")
    assert status == 200, body
    data = serve_module.json.loads(body)
    item = [x for x in data["sessions"] if x["sid"].endswith("abcd0001")]
    assert item, data
    assert item[0]["state"] in ("mirror", "acquiring", "engine")
    assert item[0]["session_id"] == item[0]["sid"]
