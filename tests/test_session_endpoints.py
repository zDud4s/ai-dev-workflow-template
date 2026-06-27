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
DASHBOARD_DIR = REPO_ROOT / ".ai" / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))
import server.transcript_paths as _tp  # noqa: E402
import server.runtime as _runtime  # noqa: E402 — BOUND_PORT + Origin allowlist live here (follows-the-move)
import server.jobs as _jobs  # noqa: E402 — the job runner / session engine (reads JOBS_DIR) lives here (follows-the-move)

SERVE_PATH = DASHBOARD_DIR / "serve.py"


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
    monkeypatch.setattr(_jobs, "JOBS_DIR", tmp_path / "jobs")  # follows-the-move: runner/factory read jobs.JOBS_DIR


@pytest.fixture(autouse=True)
def _isolate_session_lock(tmp_path, monkeypatch, serve_module):
    """Point the module SESSION_LOCK at a per-test tmp dir so lock files never
    leak into the repo's .ai/dashboard/sessions/."""
    monkeypatch.setattr(serve_module.SESSION_LOCK, "_lock_dir", tmp_path / "sessions")


@pytest.fixture
def running_server(serve_module):
    """Start the dashboard HTTP server on an ephemeral port in a thread."""
    import socketserver

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), serve_module.Handler)
    port = httpd.server_address[1]
    original_port = serve_module.PORT
    original_bound = serve_module.BOUND_PORT
    original_runtime_bound = _runtime.BOUND_PORT
    serve_module.PORT = port
    serve_module.BOUND_PORT = port
    # _origin_allowed reads BOUND_PORT from server.runtime's namespace now.
    _runtime.BOUND_PORT = port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        serve_module.PORT = original_port
        serve_module.BOUND_PORT = original_bound
        _runtime.BOUND_PORT = original_runtime_bound
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


def _arm_argv_capture(serve_module, monkeypatch):
    captured = {}
    real_popen = serve_module.subprocess.Popen
    def fake_popen(argv, **kw):
        captured["argv"] = list(argv)
        return real_popen([serve_module.sys.executable, "-c", "pass"], **kw)
    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)
    return captured


def _claude_project_slug(path: Path) -> str:
    return str(path).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-").replace(".", "-")


def _seed_projects_root(serve_module, monkeypatch, tmp_path):
    """Point transcript discovery at a tmp projects root; return the slug dir."""
    projects = tmp_path / ".claude" / "projects"
    slug = _claude_project_slug(serve_module.ROOT)
    sdir = projects / slug
    sdir.mkdir(parents=True)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    return sdir


def test_jsonl_line_emits_tool_use_with_name_and_input(serve_module):
    """An assistant turn with text + tool_use blocks must expand to one message
    event plus one tool_use event PER block, each carrying id/name/input — so
    the canvas renders a named pill with its arguments instead of an empty
    'tool' chip + '{}'."""
    line = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/x.py"}},
        {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "ls"}},
    ]}})
    evs = serve_module._jsonl_line_to_session_events(line)
    kinds = [e["kind"] for e in evs]
    assert kinds == ["message", "tool_use", "tool_use"], evs
    assert evs[0]["text"] == "Let me look."
    assert evs[1]["id"] == "tu_1" and evs[1]["name"] == "Read"
    assert evs[1]["input"] == {"file_path": "/x.py"}
    assert evs[2]["name"] == "Bash" and evs[2]["input"] == {"command": "ls"}


def test_jsonl_line_tool_result_carries_tool_use_id(serve_module):
    """A tool_result block must keep its tool_use_id so the client can bind the
    result back to the pill it belongs to."""
    line = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "is_error": False,
         "content": [{"type": "text", "text": "file contents"}]},
    ]}})
    evs = serve_module._jsonl_line_to_session_events(line)
    assert len(evs) == 1 and evs[0]["kind"] == "tool_result"
    assert evs[0]["tool_use_id"] == "tu_1"
    assert evs[0]["content"] == "file contents"
    assert evs[0]["is_error"] is False


def test_jsonl_line_emits_thinking(serve_module):
    """Thinking blocks surface as their own ``thinking`` event (chain-of-thought
    is rendered collapsed in the pane). Order is preserved: a thought that
    precedes the answer text emits before the message event."""
    line = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "let me reason"},
        {"type": "text", "text": "the answer"},
    ]}})
    evs = serve_module._jsonl_line_to_session_events(line)
    assert [e["kind"] for e in evs] == ["thinking", "message"], evs
    assert evs[0]["text"] == "let me reason"
    assert evs[0]["role"] == "assistant"
    # Blank thinking carries nothing → no event.
    blank = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "   "},
    ]}})
    assert serve_module._jsonl_line_to_session_events(blank) == []


def test_jsonl_line_skips_empty_and_unknown(serve_module):
    """Blank and unparseable/unknown lines emit nothing."""
    assert serve_module._jsonl_line_to_session_events("") == []
    assert serve_module._jsonl_line_to_session_events("not json") == []
    assert serve_module._jsonl_line_to_session_events('{"type":"weird"}') == []


def test_transcripts_dir_encodes_dot_segments(serve_module, monkeypatch, tmp_path):
    projects = tmp_path / ".claude" / "projects"
    cwd = tmp_path / "proj" / ".worktrees" / "wt"
    expected = projects / _claude_project_slug(cwd)
    expected.mkdir(parents=True)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    serve_module._TRANSCRIPTS_DIR_CACHE.clear()

    assert serve_module._transcripts_dir_for_cwd(cwd) == expected


def test_transcripts_dir_negative_cache_expires(serve_module, monkeypatch, tmp_path):
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    cwd = tmp_path / "proj" / ".worktrees" / "wt"
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    serve_module._TRANSCRIPTS_DIR_CACHE.clear()

    assert serve_module._transcripts_dir_for_cwd(cwd) is None
    key = (str(cwd), str(projects))
    cached_path, _ = serve_module._TRANSCRIPTS_DIR_CACHE[key]
    assert cached_path is None

    expected = projects / _claude_project_slug(cwd)
    expected.mkdir(parents=True)
    serve_module._TRANSCRIPTS_DIR_CACHE[key] = (
        None,
        serve_module.time.monotonic() - serve_module._TRANSCRIPTS_DIR_NEG_TTL_S - 0.1,
    )

    assert serve_module._transcripts_dir_for_cwd(cwd) == expected


def test_engine_factory_builds_resume_argv(serve_module, monkeypatch, tmp_path):
    # Resume mode is used only when the transcript already exists, so seed it.
    sdir = _seed_projects_root(serve_module, monkeypatch, tmp_path)
    (sdir / "sid-xyz.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    captured = _arm_argv_capture(serve_module, monkeypatch)

    assert hasattr(serve_module, "SESSION_REGISTRY")
    eng = serve_module._session_engine_factory("sid-xyz", "claude-sonnet-4-6")
    # The engine starts the resume job at CONSTRUCTION time (via _start_subprocess_job),
    # so argv is captured here already; submit() only feeds stdin.
    for _ in range(40):
        if "argv" in captured: break
        serve_module.time.sleep(0.05)
    assert "--resume" in captured["argv"] and "sid-xyz" in captured["argv"]
    eng.submit({"text": "oi"})


def test_engine_factory_creates_new_session_when_no_transcript(serve_module, monkeypatch, tmp_path):
    # No <sid>.jsonl on disk -> the engine must CREATE the session (--session-id),
    # not --resume a transcript that does not exist yet.
    _seed_projects_root(serve_module, monkeypatch, tmp_path)
    captured = _arm_argv_capture(serve_module, monkeypatch)

    new_sid = "12345678-1234-1234-1234-1234abcd00f1"  # intentionally no .jsonl seeded
    serve_module._session_engine_factory(new_sid, "claude-sonnet-4-6")
    for _ in range(40):
        if "argv" in captured: break
        serve_module.time.sleep(0.05)
    assert "--session-id" in captured["argv"] and new_sid in captured["argv"]
    assert "--resume" not in captured["argv"]


def test_sessions_list_merges_ide_transcripts_and_dashboard(running_server, serve_module, tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects"
    slug = _claude_project_slug(serve_module.ROOT)
    (projects / slug).mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0001"
    (projects / slug / f"{sid}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"oi"}}\n', encoding="utf-8")
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    status, body, _ = _http("GET", f"{running_server}/api/sessions")
    assert status == 200, body
    data = serve_module.json.loads(body)
    item = [x for x in data["sessions"] if x["sid"].endswith("abcd0001")]
    assert item, data
    # This IDE row has no registry entry, so its state must be the explicit
    # default ("mirror"), not just any valid state — pins the default-case branch.
    assert item[0]["state"] == "mirror"
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
    slug = _claude_project_slug(serve_module.ROOT)
    (projects / slug).mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0007"
    (projects / slug / f"{sid}.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"ola do IDE"}]}}\n',
        encoding="utf-8")
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
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


def test_session_input_acquires_resume_engine(running_server, serve_module, monkeypatch, tmp_path):
    # Resume mode requires an existing transcript, so seed one for this sid.
    sid = "aaaaaaaa-0000-1111-2222-bbbbbbbbbbbb"
    sdir = _seed_projects_root(serve_module, monkeypatch, tmp_path)
    (sdir / f"{sid}.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    captured = _arm_fake_resume_engine(serve_module, monkeypatch)
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


# ---------------------------------------------------------------------------
# Hook A — engine stdout activity (recently_active)
# ---------------------------------------------------------------------------

def test_recently_active_true_when_stdout_recent(serve_module):
    """recently_active() returns True when last_stdout_ts is within the window."""
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": "session-resume:test-sid",
            "status": "running",
            "last_stdout_ts": serve_module.time.monotonic(),  # just now
        }
    adapter = serve_module._ResumeEngineAdapter(job_id)
    assert adapter.recently_active() is True
    # cleanup
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


def test_recently_active_false_when_stdout_old(serve_module):
    """recently_active() returns False when last_stdout_ts is far in the past."""
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": "session-resume:test-sid",
            "status": "running",
            "last_stdout_ts": serve_module.time.monotonic() - 100.0,  # long ago
        }
    adapter = serve_module._ResumeEngineAdapter(job_id)
    assert adapter.recently_active() is False
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


def test_recently_active_false_when_ts_zero(serve_module):
    """recently_active() returns False when last_stdout_ts is 0.0 (not yet set)."""
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": "session-resume:test-sid",
            "status": "running",
            "last_stdout_ts": 0.0,
        }
    adapter = serve_module._ResumeEngineAdapter(job_id)
    assert adapter.recently_active() is False
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# Hook B — result event → mark_turn_done
# ---------------------------------------------------------------------------

def test_maybe_mark_session_turn_done_advances_registry(serve_module):
    """_maybe_mark_session_turn_done() calls mark_turn_done when type==result
    and the job is a session-resume job whose sid is registered."""
    import importlib
    # Use a registry session in ENGINE state with a turn in-flight.
    sid = "dddddddd-0000-0000-0000-000000000001"
    reg = serve_module.SESSION_REGISTRY
    s = reg.get_or_create(sid, jsonl_path="fake.jsonl")

    # Set up a minimal fake engine so submit() doesn't crash.
    class _FakeEng:
        def __init__(self):
            self.submitted = []
        def submit(self, turn):
            self.submitted.append(turn)
        def is_ready(self):
            return True
        def kill(self):
            pass

    fake_eng = _FakeEng()
    with s.lock:
        s.state = serve_module.session_registry.SessionState.ENGINE
        s.engine = fake_eng
        s.turn_in_flight = True
        s.pending_turn = {"text": "queued turn"}

    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": f"session-resume:{sid}",
            "session_id": sid,
            "status": "running",
            "last_stdout_ts": 0.0,
        }

    serve_module._maybe_mark_session_turn_done(job_id, {"type": "result"})

    # mark_turn_done should have cleared turn_in_flight and drained the pending turn.
    assert s.turn_in_flight is True       # True because pending_turn was drained into engine
    assert s.pending_turn is None         # pending slot emptied
    assert fake_eng.submitted            # engine.submit() was called with the queued turn

    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


def test_maybe_mark_session_turn_done_ignores_non_session_job(serve_module):
    """_maybe_mark_session_turn_done() does nothing for jobs whose task does
    not start with 'session-resume:'."""
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": "chat:some other task",
            "status": "running",
            "last_stdout_ts": 0.0,
        }
    # Should not raise and should not affect the registry.
    serve_module._maybe_mark_session_turn_done(job_id, {"type": "result"})
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


def test_maybe_mark_session_turn_done_noop_non_result(serve_module):
    """_maybe_mark_session_turn_done() is a no-op when type != 'result'."""
    sid = "dddddddd-0000-0000-0000-000000000002"
    reg = serve_module.SESSION_REGISTRY
    s = reg.get_or_create(sid, jsonl_path="fake2.jsonl")
    with s.lock:
        s.state = serve_module.session_registry.SessionState.ENGINE
        s.turn_in_flight = True

    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": f"session-resume:{sid}",
            "session_id": sid,
            "status": "running",
            "last_stdout_ts": 0.0,
        }
    serve_module._maybe_mark_session_turn_done(job_id, {"type": "text_delta"})
    # turn_in_flight must remain True; mark_turn_done was not called.
    assert s.turn_in_flight is True
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# ForeignWriteWatcher + SessionLock wiring tests (Part A & B)
# ---------------------------------------------------------------------------

def test_foreign_write_watcher_detects_growth(serve_module, tmp_path, _reset_session_registry):
    """poll_once() marks a MIRROR session FOREIGN when the .jsonl file grows."""
    sid = "eeeeeeee-0000-0000-0000-000000000010"
    jsonl = tmp_path / f"{sid}.jsonl"
    jsonl.write_bytes(b"")  # create empty file

    reg = serve_module.SESSION_REGISTRY
    s = reg.get_or_create(sid, jsonl_path=str(jsonl))
    # Seed last_growth_ts to 0 so elapsed is large (file was quiet long ago),
    # but last_size to 0 so any new bytes count as growth, not a quiet tick.
    s.last_growth_ts = 0.0
    s.last_size = 0

    # Write bytes so os.stat() sees growth.
    jsonl.write_bytes(b'{"type":"user"}\n')

    watcher = serve_module.ForeignWriteWatcher()
    watcher.poll_once()

    assert s.state == serve_module.session_registry.SessionState.FOREIGN, (
        f"expected FOREIGN after file growth, got {s.state}"
    )


def test_foreign_write_watcher_gone_terminates(serve_module, tmp_path, _reset_session_registry):
    """poll_once() sets terminated=True when the .jsonl file is absent."""
    sid = "eeeeeeee-0000-0000-0000-000000000011"
    missing = tmp_path / f"{sid}.jsonl"
    # Do NOT create the file — it is intentionally absent.

    reg = serve_module.SESSION_REGISTRY
    s = reg.get_or_create(sid, jsonl_path=str(missing))

    watcher = serve_module.ForeignWriteWatcher()
    watcher.poll_once()

    # note_jsonl_gone() calls _reconcile_to_mirror which sets terminated=True.
    assert s.terminated is True, "expected terminated=True after file gone"
    assert s.state == serve_module.session_registry.SessionState.MIRROR, (
        f"expected MIRROR after file gone, got {s.state}"
    )


def test_lock_blocks_spawn(serve_module, tmp_path, _reset_session_registry):
    """submit_turn() with lock_acquire=False must not spawn an engine."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(serve_module.__file__).parent / "scripts"))
    import session_registry as sr

    spawn_calls = []

    def fake_factory(sid, model):
        spawn_calls.append(sid)
        # Return a minimal fake engine that is immediately ready.
        class _FE:
            def submit(self, t): pass
            def is_ready(self): return True
            def kill(self): pass
        return _FE()

    # Registry with lock_acquire always returning False — spawn must be blocked.
    reg_blocked = sr.SessionRegistry(
        engine_factory=fake_factory,
        lock_acquire=lambda sid, owner: False,
    )
    s_blocked = reg_blocked.get_or_create("lock-sid-1", jsonl_path=str(tmp_path / "a.jsonl"))
    # Seed so the quiescence check passes (elapsed >= QUIESCENT_S).
    s_blocked.last_growth_ts = 0.0
    result = reg_blocked.submit_turn("lock-sid-1", {"text": "hello"}, model="m")
    # Engine must NOT have been spawned.
    assert len(spawn_calls) == 0, f"engine spawned despite lock_acquire=False: {spawn_calls}"
    # A warning must have been recorded.
    assert any("lock" in w for w in s_blocked.warnings), (
        f"no lock warning in session warnings: {s_blocked.warnings}"
    )

    # With lock_acquire=True the engine SHOULD spawn.
    spawn_calls.clear()
    reg_allowed = sr.SessionRegistry(
        engine_factory=fake_factory,
        lock_acquire=lambda sid, owner: True,
    )
    s_allowed = reg_allowed.get_or_create("lock-sid-2", jsonl_path=str(tmp_path / "b.jsonl"))
    s_allowed.last_growth_ts = 0.0
    reg_allowed.submit_turn("lock-sid-2", {"text": "hello"}, model="m")
    assert len(spawn_calls) == 1, f"engine not spawned when lock_acquire=True: {spawn_calls}"


def test_release_calls_lock_release(serve_module, tmp_path, _reset_session_registry):
    """release() must call lock_release when the hook is set."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(serve_module.__file__).parent / "scripts"))
    import session_registry as sr

    released = []

    def fake_factory(sid, model):
        class _FE:
            def submit(self, t): pass
            def is_ready(self): return True
            def kill(self): pass
        return _FE()

    reg = sr.SessionRegistry(
        engine_factory=fake_factory,
        lock_acquire=lambda sid, owner: True,
        lock_release=lambda sid: released.append(sid),
    )
    s = reg.get_or_create("release-sid", jsonl_path=str(tmp_path / "r.jsonl"))
    s.last_growth_ts = 0.0
    reg.submit_turn("release-sid", {"text": "hi"}, model="m")

    reg.release("release-sid")

    assert "release-sid" in released, (
        f"lock_release not called after release(); got: {released}"
    )


# ---------------------------------------------------------------------------
# Probe wiring — engine_active_probe set on acquire
# ---------------------------------------------------------------------------

def test_probe_wired_after_submit_turn_acquire(serve_module, monkeypatch):
    """After submit_turn() acquires and sets s.engine, s.engine_active_probe
    must be set to the engine's recently_active method (or None via getattr
    for engines that lack it)."""
    sid = "eeeeeeee-0000-0000-0000-000000000003"

    class _FakeEngineWithProbe:
        def recently_active(self):
            return True
        def is_ready(self):
            return False  # stay in ACQUIRING so we can inspect
        def submit(self, turn):
            pass
        def kill(self):
            pass

    fake_eng = _FakeEngineWithProbe()
    monkeypatch.setattr(
        serve_module.SESSION_REGISTRY,
        "_engine_factory",
        lambda s, m: fake_eng,
    )

    reg = serve_module.SESSION_REGISTRY
    # Ensure a session exists so submit_turn can find it.
    s = reg.get_or_create(sid, jsonl_path="fake3.jsonl")
    # Force MIRROR + quiescent so the factory is called.
    with s.lock:
        s.state = serve_module.session_registry.SessionState.MIRROR
        s.last_growth_ts = 0.0

    reg.submit_turn(sid, {"text": "hello"}, model="claude-sonnet-4-6")

    # engine_active_probe must point to the engine's recently_active method.
    assert s.engine_active_probe is not None
    assert callable(s.engine_active_probe)
    assert s.engine_active_probe() is True


# ---------------------------------------------------------------------------
# Part 1 — input result maps to 200 / 202 / 409
# ---------------------------------------------------------------------------

def test_session_input_accepted_returns_200(running_server, serve_module, monkeypatch):
    """submit_turn returning 'accepted' must yield HTTP 200 with status=accepted."""
    _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "aaaaaaaa-1111-2222-3333-aaaaaaaaaaaa"
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/input",
        data=b'{"text":"hello"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body
    assert serve_module.json.loads(body)["status"] == "accepted"


def test_session_input_queued_returns_202(running_server, serve_module, monkeypatch):
    """When the session already has a turn in-flight, submit_turn returns
    'queued' — the endpoint must respond 202 with status=queued."""
    _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
    reg = serve_module.SESSION_REGISTRY
    # Create the session and force it into ENGINE state with a turn in-flight.
    s = reg.get_or_create(sid, jsonl_path="fake_q.jsonl")
    with s.lock:
        s.state = serve_module.session_registry.SessionState.ENGINE
        s.turn_in_flight = True   # keeps next submit_turn in the 'queued' branch
        # Give it a minimal fake engine so submit() doesn't crash.
        class _FakeEngQ:
            def submit(self, t): pass
            def is_ready(self): return True
            def kill(self): pass
        s.engine = _FakeEngQ()
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/input",
        data=b'{"text":"second turn"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 202, body
    assert serve_module.json.loads(body)["status"] == "queued"


def test_session_input_rejected_returns_409(running_server, serve_module, monkeypatch):
    """When the pending slot is already occupied, submit_turn returns 'rejected'
    — the endpoint must respond 409 with status=already_queued."""
    _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "aaaaaaaa-1111-2222-3333-cccccccccccc"
    reg = serve_module.SESSION_REGISTRY
    # Create the session and pre-fill the pending slot so submit_turn rejects.
    s = reg.get_or_create(sid, jsonl_path="fake_r.jsonl")
    with s.lock:
        s.state = serve_module.session_registry.SessionState.ENGINE
        s.turn_in_flight = True
        s.pending_turn = {"text": "already queued"}  # slot occupied → rejected
        class _FakeEngR:
            def submit(self, t): pass
            def is_ready(self): return True
            def kill(self): pass
        s.engine = _FakeEngR()
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/input",
        data=b'{"text":"overflow turn"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 409, body
    assert serve_module.json.loads(body)["status"] == "already_queued"


def test_session_input_owner_forwarded(running_server, serve_module, monkeypatch):
    """An 'owner' field in the body must be accepted and passed through without
    causing an error (forward-compat for multi-tab; registry ignores unknown
    owners when no conflict exists)."""
    _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "aaaaaaaa-1111-2222-3333-dddddddddddd"
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/input",
        data=b'{"text":"hi","owner":"tab-abc-1"}',
        headers={"Content-Type": "application/json"},
    )
    # 200 or 202 are both fine (accepted or queued); not an error.
    assert status in (200, 202), body


def test_session_input_owner_invalid_rejected(running_server, serve_module, monkeypatch):
    """An 'owner' value that fails the short-id guard must be rejected with 400."""
    sid = "aaaaaaaa-1111-2222-3333-eeeeeeeeeeee"
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/input",
        data=b'{"text":"hi","owner":"bad owner with spaces!"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 400, body


# ---------------------------------------------------------------------------
# Part 2 — interrupt endpoint
# ---------------------------------------------------------------------------

def test_session_interrupt_returns_200_unknown_sid(running_server, serve_module):
    """POST /api/sessions/<uuid>/interrupt on an unknown session must respond
    200 (idempotent) rather than 404 — interrupting a gone session is a no-op."""
    sid = "ffffffff-0000-0000-0000-000000000001"
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/interrupt",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body
    assert serve_module.json.loads(body)["status"] == "interrupted"


def test_session_interrupt_bad_sid_returns_400(running_server, serve_module):
    """A non-UUID sid on the interrupt endpoint must return 400."""
    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/not-a-uuid/interrupt",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400, body


def test_session_interrupt_calls_engine_interrupt(running_server, serve_module, monkeypatch):
    """When a session has a live engine, interrupt() must be called on it and
    SESSION_REGISTRY.interrupt(sid) must reconcile state."""
    _arm_fake_resume_engine(serve_module, monkeypatch)
    sid = "ffffffff-0000-0000-0000-000000000002"
    reg = serve_module.SESSION_REGISTRY

    # Track engine.interrupt() calls via a fake engine attached to the session.
    interrupted = []

    class _FakeEngInt:
        def interrupt(self):
            interrupted.append(True)
        def submit(self, t): pass
        def is_ready(self): return True
        def kill(self): pass

    s = reg.get_or_create(sid, jsonl_path="fake_int.jsonl")
    with s.lock:
        s.state = serve_module.session_registry.SessionState.ENGINE
        s.turn_in_flight = True
        s.engine = _FakeEngInt()

    status, body, _ = _http(
        "POST", f"{running_server}/api/sessions/{sid}/interrupt",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body
    assert serve_module.json.loads(body)["status"] == "interrupted"
    # engine.interrupt() must have been invoked.
    assert interrupted, "engine.interrupt() was not called"
    # Registry reconcile must have cleared turn_in_flight.
    assert s.turn_in_flight is False, "registry.interrupt() did not reconcile state"


# ---------------------------------------------------------------------------
# New signals: pending flag + conflict warnings on the session stream
# ---------------------------------------------------------------------------

def test_source_guard_pending_and_warnings(serve_module):
    """Source-level guard: _handle_session_stream must contain the symbols that
    implement the pending flag and warning drain.  Non-flaky — fails before the
    implementation is written, passes immediately after."""
    src = inspect.getsource(serve_module.Handler._handle_session_stream)
    assert '"pending"' in src, "pending key missing from state_change frame"
    assert "last_emitted_pending" in src, "last_emitted_pending tracker missing"
    assert ".warnings" in src, "warning drain (.warnings) missing"


def test_stream_leading_frame_carries_pending(running_server, serve_module, tmp_path, monkeypatch):
    """Behavioral: the very first SSE frame must include a 'pending' key.

    The test seeds a .jsonl so the stream opens, pre-seeds the session in the
    registry with pending_turn set, then reads the leading state_change frame and
    asserts 'pending' is present and True.  Because the leading frame is emitted
    synchronously before any file I/O, this test does not depend on timing."""
    projects = tmp_path / ".claude" / "projects"
    slug = _claude_project_slug(serve_module.ROOT)
    (projects / slug).mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0020"
    (projects / slug / f"{sid}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    # Put the session in the registry with a pending turn so the leading frame
    # should report pending=True.
    reg = serve_module.SESSION_REGISTRY
    s = reg.get_or_create(sid, jsonl_path=str(projects / slug / f"{sid}.jsonl"))
    with s.lock:
        s.pending_turn = {"text": "queued message"}

    buf = _read_sse(running_server, f"/api/sessions/{sid}/stream",
                    until=b'"pending"', timeout=6)
    assert b"text/event-stream" in buf.lower(), "not an SSE response"
    _, _, body = buf.partition(b"\r\n\r\n")
    frames = [ln[len(b"data:"):].strip()
              for ln in body.split(b"\n") if ln.startswith(b"data:")]
    assert frames, f"no SSE data frames received; raw buf: {buf!r}"
    first = serve_module.json.loads(frames[0])
    assert first["kind"] == "state_change", f"first frame is not state_change: {first}"
    assert "pending" in first, f"'pending' key missing from leading state_change frame: {first}"
    assert first["pending"] is True, f"expected pending=True (turn was queued), got: {first}"


def test_stream_emits_warning_frame(running_server, serve_module, tmp_path, monkeypatch):
    """Behavioral: when a session has warnings queued, the stream must emit at
    least one SSE frame with kind='warning' and the warning text.

    Warnings are drained on each poll tick.  We append the warning before the
    stream opens, then read until we see 'warning' in the byte stream."""
    projects = tmp_path / ".claude" / "projects"
    slug = _claude_project_slug(serve_module.ROOT)
    (projects / slug).mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0021"
    (projects / slug / f"{sid}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    # Pre-seed a warning on the session so the first poll tick drains it.
    reg = serve_module.SESSION_REGISTRY
    s = reg.get_or_create(sid, jsonl_path=str(projects / slug / f"{sid}.jsonl"))
    warning_text = "ceded: foreign write during idle engine"
    with serve_module.SESSION_REGISTRY._lock:
        s.warnings.append(warning_text)

    # Read with a generous timeout; the warning is drained on the first poll tick
    # (up to 1 s after the leading frame).
    buf = _read_sse(running_server, f"/api/sessions/{sid}/stream",
                    until=b'"warning"', timeout=8)
    assert b"text/event-stream" in buf.lower(), "not an SSE response"
    _, _, body = buf.partition(b"\r\n\r\n")
    frames = [ln[len(b"data:"):].strip()
              for ln in body.split(b"\n") if ln.startswith(b"data:")]
    warning_frames = []
    for f in frames:
        try:
            obj = serve_module.json.loads(f)
            if obj.get("kind") == "warning":
                warning_frames.append(obj)
        except Exception:
            pass
    assert warning_frames, (
        f"no warning frame found in SSE stream; frames: {frames!r}"
    )
    assert any(warning_text in wf.get("text", "") for wf in warning_frames), (
        f"warning text not in any warning frame: {warning_frames}"
    )


# ---------------------------------------------------------------------------
# Review follow-ups: adapter reports send failures (I2), corroboration window
# spans multiple watch ticks (I3), and warnings are not cleared from the shared
# list so concurrent streams each receive them (I4).
# ---------------------------------------------------------------------------

def test_resume_adapter_submit_reports_send_failure(serve_module, monkeypatch):
    """_ResumeEngineAdapter.submit must return False when the stdin write fails
    and True when it succeeds, so the registry can fail safe instead of wedging."""
    # _ResumeEngineAdapter moved to server/jobs.py, so submit() resolves
    # _send_to_stdin in jobs.py's namespace — patch there (follows-the-move).
    monkeypatch.setattr(_jobs, "_send_to_stdin", lambda j, t: (False, "job not running"))
    assert serve_module._ResumeEngineAdapter("job-dead").submit({"text": "hi"}) is False

    monkeypatch.setattr(_jobs, "_send_to_stdin", lambda j, t: (True, ""))
    assert serve_module._ResumeEngineAdapter("job-live").submit({"text": "hi"}) is True


def test_corroboration_window_covers_multiple_watch_ticks(serve_module):
    """The stdout-corroboration window must span at least three watcher ticks so
    normal scheduler jitter cannot make the engine mis-cede its own trailing bytes."""
    assert serve_module.STDOUT_CORROBORATION_WINDOW_S >= 3 * serve_module.WATCH_INTERVAL_S


def test_session_stream_does_not_clear_shared_warnings(serve_module):
    """Warnings must be delivered via a per-stream cursor, never cleared from the
    shared list — otherwise the first of two concurrent streams on the same
    session consumes a warning and the second never sees it."""
    src = inspect.getsource(serve_module.Handler)
    assert ".warnings.clear()" not in src, "the SSE loop must not clear the shared warnings list"
    assert "warn_seen" in src, "the SSE loop should track a per-stream warning cursor"


# ---------------------------------------------------------------------------
# Branch groundwork: capture the forked session id from a chat fork job's
# init event (the stdout pump only captured it for codex before).
# ---------------------------------------------------------------------------

def test_forked_chat_job_captures_new_session_id(serve_module):
    job_id = "job-fork-cap-1"
    src = "11111111-1111-1111-1111-111111111111"
    new = "22222222-2222-2222-2222-222222222222"
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {"id": job_id, "kind": "chat",
                                     "session_id": src, "forked_from": src}
    try:
        serve_module._maybe_capture_forked_sid(
            job_id, "chat", {"type": "system", "subtype": "init", "session_id": new})
        with serve_module.JOBS_LOCK:
            assert serve_module.JOBS[job_id]["session_id"] == new
    finally:
        with serve_module.JOBS_LOCK:
            serve_module.JOBS.pop(job_id, None)


def test_non_fork_chat_job_keeps_sid_on_init(serve_module):
    """A plain resume (non-fork) chat job must NOT have its sid overwritten."""
    job_id = "job-resume-cap-1"
    src = "33333333-3333-3333-3333-333333333333"
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {"id": job_id, "kind": "chat", "session_id": src}
    try:
        serve_module._maybe_capture_forked_sid(
            job_id, "chat", {"type": "system", "session_id": "99999999-9999-9999-9999-999999999999"})
        with serve_module.JOBS_LOCK:
            assert serve_module.JOBS[job_id]["session_id"] == src
    finally:
        with serve_module.JOBS_LOCK:
            serve_module.JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# Branch endpoint: copy a session's transcript under a fresh sid on disk. No
# subprocess / no --fork-session, so the branch can neither time out (#3) nor
# truncate the new transcript (#4); the new pane resumes the copy on input.
# ---------------------------------------------------------------------------

def test_session_branch_copies_transcript_with_new_sid(running_server, serve_module, monkeypatch, tmp_path):
    sdir = _seed_projects_root(serve_module, monkeypatch, tmp_path)
    src = "12345678-1234-1234-1234-1234abcd0aa1"
    records = [
        {"type": "summary", "sessionId": src, "summary": "x"},
        {"type": "user", "sessionId": src, "uuid": "u1", "parentUuid": None},
        {"type": "assistant", "sessionId": src, "uuid": "u2", "parentUuid": "u1"},
    ]
    src_path = sdir / f"{src}.jsonl"
    src_path.write_text(
        "\n".join(serve_module.json.dumps(r) for r in records) + "\n", encoding="utf-8")

    status, body, _ = _http("POST", f"{running_server}/api/sessions/{src}/branch", data=b"{}")
    assert status == 200, body
    new = serve_module.json.loads(body)["sid"]
    assert serve_module.Handler._UUID_RE.match(new) and new != src

    dst_path = sdir / f"{new}.jsonl"
    assert dst_path.is_file(), "the branched transcript must be written to disk"
    out = [serve_module.json.loads(line)
           for line in dst_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # Every record's sessionId is rewritten to the new sid; no record dropped.
    assert len(out) == len(records)
    assert all(r["sessionId"] == new for r in out)
    # Per-message parent/uuid links survive the copy unchanged.
    assert out[2]["parentUuid"] == "u1" and out[2]["uuid"] == "u2"
    # The source transcript is left untouched.
    src_out = [serve_module.json.loads(line)
               for line in src_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(r["sessionId"] == src for r in src_out)
    # The atomic write leaves no stray temp file behind.
    assert not list(sdir.glob("*.jsonl.tmp"))


def test_session_branch_404_when_no_transcript(running_server, serve_module, monkeypatch, tmp_path):
    _seed_projects_root(serve_module, monkeypatch, tmp_path)
    missing = "abcdef01-0000-0000-0000-000000000000"
    status, body, _ = _http("POST", f"{running_server}/api/sessions/{missing}/branch", data=b"{}")
    assert status == 404, body


def test_session_branch_rejects_bad_sid(running_server, serve_module):
    status, body, _ = _http("POST", f"{running_server}/api/sessions/not-a-uuid/branch", data=b"{}")
    assert status == 400
