"""Tests for SessionRegistry integration in serve.py (Task 5).

Verifies that SESSION_REGISTRY is exposed at module level and that
_session_engine_factory spawns a ``claude --resume <sid>`` subprocess
backed by _start_subprocess_job / _build_chat_argv.
"""

from __future__ import annotations

import importlib.util
import inspect
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


def _read_sse(base_url, path, until: bytes, timeout=4):
    """Open the SSE via a raw socket and read until `until` is seen (or timeout)."""
    from urllib.parse import urlparse
    import socket, time as _t
    p = urlparse(base_url + path)
    sock = socket.create_connection((p.hostname, p.port), timeout=5)
    try:
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {p.hostname}:{p.port}\r\n"
                      f"Accept: text/event-stream\r\n\r\n").encode("utf-8"))
        sock.settimeout(timeout)
        buf = b""; deadline = _t.time() + timeout
        while _t.time() < deadline:
            try: chunk = sock.recv(4096)
            except socket.timeout: break
            if not chunk: break
            buf += chunk
            if until in buf: break
        return buf
    finally:
        sock.close()


def test_session_stream_tails_jsonl_in_mirror(running_server, serve_module, tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects"
    slug = str(serve_module.ROOT).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    (projects / slug).mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0007"
    (projects / slug / f"{sid}.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ola do IDE"}]}}\n',
        encoding="utf-8")
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    buf = _read_sse(running_server, f"/api/sessions/{sid}/stream", until=b"ola do IDE")
    assert b"text/event-stream" in buf.lower()
    assert b"ola do IDE" in buf
    _, _, body = buf.partition(b"\r\n\r\n")
    frames = [ln[len(b"data:"):].strip() for ln in body.split(b"\n") if ln.startswith(b"data:")]
    assert frames, body
    first = serve_module.json.loads(frames[0])
    assert first["kind"] == "state_change" and first["state"] == "mirror"

def test_session_stream_404_unknown(running_server):
    status, body, _ = _http("GET", f"{running_server}/api/sessions/00000000-0000-0000-0000-000000000000/stream")
    assert status == 404, body


def _arm_fake_resume_engine(serve_module, monkeypatch):
    """Monkeypatch Popen to a stand-in that reads stdin (keeps the job alive) and
    captures the argv. Returns the `captured` dict."""
    captured = {}
    real_popen = serve_module.subprocess.Popen
    def fake_popen(argv, **kw):
        captured["argv"] = list(argv)
        return real_popen([serve_module.sys.executable, "-u", "-c",
                           "import sys; sys.stdin.readline()"], **kw)
    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)
    return captured


def test_session_input_acquires_resume_engine(running_server, serve_module, monkeypatch):
    captured = _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "aaaaaaaa-0000-1111-2222-bbbbbbbbbbbb"
    status, body, _ = _http("POST", f"{running_server}/api/sessions/{sid}/input",
                            data=b'{"text":"continua por favor"}',
                            headers={"Content-Type": "application/json"})
    assert status in (200, 202), body
    assert serve_module.json.loads(body)["status"] == "accepted"
    for _ in range(40):
        if "argv" in captured: break
        serve_module.time.sleep(0.05)
    assert "--resume" in captured["argv"] and sid in captured["argv"]

def test_session_release_returns_to_mirror(running_server, serve_module, monkeypatch):
    sid = "aaaaaaaa-0000-1111-2222-cccccccccccc"
    _arm_fake_resume_engine(serve_module, monkeypatch)
    _http("POST", f"{running_server}/api/sessions/{sid}/input", data=b'{"text":"x"}',
          headers={"Content-Type": "application/json"})
    status, body, _ = _http("POST", f"{running_server}/api/sessions/{sid}/release", data=b"{}",
                            headers={"Content-Type": "application/json"})
    assert status == 200, body
    s = serve_module.SESSION_REGISTRY.get_or_create(sid, jsonl_path="x")
    assert s.state == serve_module.session_registry.SessionState.MIRROR

def test_session_input_400_empty_text(running_server):
    sid = "aaaaaaaa-0000-1111-2222-dddddddddddd"
    status, body, _ = _http("POST", f"{running_server}/api/sessions/{sid}/input",
                            data=b"{}", headers={"Content-Type": "application/json"})
    assert status == 400, body

def test_session_input_rejects_non_uuid_sid(running_server):
    status, body, _ = _http("POST", f"{running_server}/api/sessions/not-a-uuid/input",
                            data=b'{"text":"x"}', headers={"Content-Type": "application/json"})
    assert status in (400, 404), body

def test_session_input_rejects_bad_model(running_server):
    """A body-provided model must be validated before reaching the subprocess argv
    (mirrors _handle_jobs_create's model guard). No engine should be spawned."""
    sid = "aaaaaaaa-0000-1111-2222-eeeeeeeeeeee"
    status, body, _ = _http("POST", f"{running_server}/api/sessions/{sid}/input",
                            data=b'{"text":"x","model":"--evil flag"}',
                            headers={"Content-Type": "application/json"})
    assert status == 400, body


def test_session_input_promotes_to_engine(running_server, serve_module, monkeypatch):
    """Regression (webapp-validation): the resume subprocess is launched on a
    worker thread, so submit_turn()'s synchronous engine.is_ready() check
    essentially never catches it. A background waiter in _session_engine_factory
    must promote ACQUIRING -> ENGINE once the process is up (which flushes the
    buffered first turn into the engine). Without it the session dwells in
    ACQUIRING forever and the dashboard can never continue the conversation."""
    _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "aaaaaaaa-0000-1111-2222-f00000000001"
    status, body, _ = _http("POST", f"{running_server}/api/sessions/{sid}/input",
                            data=b'{"text":"continue please"}',
                            headers={"Content-Type": "application/json"})
    assert status in (200, 202), body
    s = serve_module.SESSION_REGISTRY.get_or_create(sid, jsonl_path="x")
    engine_state = serve_module.session_registry.SessionState.ENGINE
    deadline = serve_module.time.monotonic() + 8
    while serve_module.time.monotonic() < deadline:
        if s.state == engine_state:
            break
        serve_module.time.sleep(0.05)
    assert s.state == engine_state, f"session stuck in {s.state}; promotion never fired"


def test_session_stream_pushes_state_transitions(serve_module):
    """Regression (webapp-validation): _handle_session_stream emitted only the
    leading state_change frame, so an already-open stream never saw the session
    go live (chip stuck on 'mirror'). Its tail loop must poll the registry and
    emit a fresh state_change whenever the state changes."""
    src = inspect.getsource(serve_module.Handler._handle_session_stream)
    assert "last_emitted_state" in src
    assert "SESSION_REGISTRY._lock" in src
    assert "_cur_state != last_emitted_state" in src
    assert '"kind": "state_change"' in src
