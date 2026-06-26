"""Tests for the multi-terminal dashboard job streaming + stdin features.

The dashboard server (`.ai/dashboard/serve.py`) needs to:

  1. Spawn jobs with an open stdin pipe so the user can talk to a running
     agent from the dashboard (not just watch its output).
  2. Expose POST /api/jobs/<id>/input  -> writes one line to that stdin.
  3. Expose GET  /api/jobs/<id>/stream -> Server-Sent Events of log chunks
     as the subprocess writes them, so multiple terminal panes can run in
     real time in the browser without polling.
  4. Support an interactive ``chat`` job kind that spawns ``claude`` in
     ``--input-format stream-json --output-format stream-json`` mode, so
     the operator can have a back-and-forth conversation with Claude from
     the browser instead of one-shot ``-p`` prompts.

These tests do NOT depend on the `claude` CLI being installed; they inject
a controllable Python subprocess into the in-memory JOBS registry via the
new `_start_subprocess_job` helper.
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
DASHBOARD_DIR = REPO_ROOT / ".ai" / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))
import server.transcript_paths as _tp  # noqa: E402

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
    """Redirect ``serve_module.JOBS_DIR`` to a per-test tmp directory.

    Without this, any test that calls ``_start_subprocess_job`` writes a
    real ``.log`` file under ``.ai/dashboard/jobs/`` — the same dir the
    running dashboard reads. Those leftover synthetic fixtures (``task:
    cost test``, ``task: (noop)``, etc.) then show up in the operator's
    job picker and render as confusing empty panes when opened.
    """
    monkeypatch.setattr(serve_module, "JOBS_DIR", tmp_path / "jobs")


@pytest.fixture
def running_server(serve_module):
    """Start the dashboard HTTP server on an ephemeral port in a thread.

    Monkeypatches BOTH ``serve.PORT`` AND ``serve.BOUND_PORT`` so the
    CSRF/Origin allowlist (which keys on ``BOUND_PORT``) accepts the
    ephemeral port we just bound. Without this every POST returns 403
    "origin not allowed" even with the right Origin header.
    """
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
    # CSRF guard accepts them. Tests that want to probe origin-rejection
    # explicitly can override by passing Origin themselves in ``headers``.
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


def _inject_python_job(serve_module, script: str) -> str:
    """Spawn a python subprocess via the new helper and register it.

    The script runs `python -u -c <script>` so output is line-buffered.
    Returns the job_id.
    """
    job_id = str(uuid.uuid4())
    argv = [sys.executable, "-u", "-c", script]
    serve_module._start_subprocess_job(
        job_id=job_id,
        kind="orchestrate",
        task="test job",
        argv=argv,
    )
    # Wait briefly for runner thread to flip status and set pid.
    for _ in range(50):
        with serve_module.JOBS_LOCK:
            j = serve_module.JOBS.get(job_id)
            if j and j.get("pid"):
                break
        time.sleep(0.05)
    return job_id


# ----- input endpoint -------------------------------------------------------


def test_input_endpoint_writes_line_to_subprocess_stdin(running_server, serve_module):
    """POST /api/jobs/<id>/input forwards `text` to the subprocess stdin.

    The test subprocess echoes each stdin line prefixed with 'got:' then exits.
    """
    job_id = _inject_python_job(
        serve_module,
        "import sys\n"
        "line = sys.stdin.readline()\n"
        "sys.stdout.write('got:' + line)\n"
        "sys.stdout.flush()\n",
    )
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/{job_id}/input",
        data=b'{"text": "hello"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body

    # Wait for subprocess to finish (it exits after one line).
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            j = serve_module.JOBS[job_id]
            if j["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)

    log_path = Path(serve_module.JOBS[job_id]["log_path"])
    log = log_path.read_text(encoding="utf-8")
    assert "got:hello" in log, f"subprocess did not see input; log was:\n{log}"


def test_input_endpoint_404_for_unknown_job(running_server):
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/00000000-0000-0000-0000-000000000000/input",
        data=b'{"text": "x"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 404, body


def test_input_endpoint_409_for_finished_job(running_server, serve_module):
    job_id = _inject_python_job(serve_module, "pass\n")
    # Wait for finish.
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id]["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/{job_id}/input",
        data=b'{"text": "hi"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 409, body


def test_input_endpoint_requires_text(running_server, serve_module):
    job_id = _inject_python_job(
        serve_module,
        "import sys; sys.stdin.readline()\n",  # keep it alive a moment
    )
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/{job_id}/input",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400, body


# ----- SSE stream endpoint --------------------------------------------------


def test_stream_endpoint_emits_log_chunks_as_sse(running_server, serve_module):
    """GET /api/jobs/<id>/stream is an SSE stream that emits `data: <chunk>` frames."""
    job_id = _inject_python_job(
        serve_module,
        "import sys, time\n"
        "for i in range(3):\n"
        "    sys.stdout.write(f'line{i}\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n",
    )

    # Open the stream and read frames until we've collected all 3 lines or timeout.
    parsed = urlparse(f"{running_server}/api/jobs/{job_id}/stream")
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(
            f"GET /api/jobs/{job_id}/stream HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode("utf-8"),
        )
        sock.settimeout(3)
        buf = b""
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"line0" in buf and b"line1" in buf and b"line2" in buf:
                break
    finally:
        sock.close()

    text = buf.decode("utf-8", errors="replace")
    # The response must be SSE (correct Content-Type and `data:` frames).
    assert "text/event-stream" in text.lower(), text[:400]
    assert "line0" in text and "line1" in text and "line2" in text, text[-400:]
    assert "data:" in text, text[-400:]


def test_stream_endpoint_404_for_unknown_job(running_server):
    status, body, _ = _http(
        "GET",
        f"{running_server}/api/jobs/00000000-0000-0000-0000-000000000000/stream",
    )
    assert status == 404, body


# ----- line-ending normalization -------------------------------------------


def test_log_file_has_no_doubled_newlines_or_stray_cr(serve_module):
    """The log file must contain exactly the bytes the subprocess wrote
    (after stripping the job header), with no platform line-ending doubling
    and no stray ``\\r`` characters. This guards against the Windows bug
    where opening the log file in text mode re-translates ``\\n`` to ``\\r\\n``,
    producing ``\\r\\r\\n`` on disk and ``\\n\\n`` on read-back.
    """
    job_id = _inject_python_job(
        serve_module,
        "import sys\n"
        "sys.stdout.write('alpha\\n')\n"
        "sys.stdout.write('beta\\n')\n"
        "sys.stdout.write('gamma\\n')\n"
        "sys.stdout.flush()\n",
    )
    # Wait for the subprocess to finish.
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id]["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)

    log_path = Path(serve_module.JOBS[job_id]["log_path"])
    raw = log_path.read_bytes()
    # Strip the header (everything up to and including the blank line after it).
    body = raw.split(b"\n\n", 1)[-1]
    assert body == b"alpha\nbeta\ngamma\n", repr(body)


def test_stream_chunks_do_not_contain_stray_cr(running_server, serve_module):
    """SSE frames sent to the browser must contain LF-only line endings.
    A stray ``\\r`` from a Windows subprocess (where Python's stdout
    automatically converts ``\\n`` to ``\\r\\n`` when stdout is a pipe in
    text mode) shows up as a visible artifact in the terminal pane.
    """
    job_id = _inject_python_job(
        serve_module,
        "import sys\n"
        "sys.stdout.write('one\\n')\n"
        "sys.stdout.write('two\\n')\n"
        "sys.stdout.flush()\n",
    )
    parsed = urlparse(f"{running_server}/api/jobs/{job_id}/stream")
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(
            f"GET /api/jobs/{job_id}/stream HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode("utf-8"),
        )
        sock.settimeout(3)
        buf = b""
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"event: end" in buf:
                break
    finally:
        sock.close()

    # Split off the HTTP headers (they legitimately use CRLF).
    head, _, body = buf.partition(b"\r\n\r\n")
    # In the SSE body, the only \r allowed is none — payload lines end with \n.
    assert b"\r" not in body, f"stray CR in stream body: {body!r}"
    # Both payload lines must be present.
    assert b"one" in body and b"two" in body, body


# ----- chat mode (interactive claude via stream-json) ----------------------


def test_build_chat_argv_uses_stream_json_protocol(serve_module):
    """``_build_chat_argv`` must produce the exact CLI invocation needed to
    talk to ``claude`` interactively via JSON on stdin/stdout. Each flag is
    load-bearing — drop one and the chat protocol stops working."""
    argv = serve_module._build_chat_argv(
        model="claude-sonnet-4-6",
        session_id="11111111-1111-1111-1111-111111111111",
    )
    # First arg is the executable; the rest are flags we care about.
    flags = argv[1:]
    assert "--print" in flags
    assert flags[flags.index("--input-format") + 1] == "stream-json"
    assert flags[flags.index("--output-format") + 1] == "stream-json"
    assert flags[flags.index("--model") + 1] == "claude-sonnet-4-6"
    assert flags[flags.index("--session-id") + 1] == "11111111-1111-1111-1111-111111111111"


def test_chat_user_message_is_json_envelope(serve_module):
    """``_chat_user_message`` must emit one JSON-encoded line ending in ``\\n``
    matching the SDK stream-json input schema, so ``claude --input-format
    stream-json`` accepts it as a user turn."""
    raw = serve_module._chat_user_message("hello world")
    assert isinstance(raw, bytes)
    assert raw.endswith(b"\n")
    obj = json.loads(raw)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"] == [{"type": "text", "text": "hello world"}]


def test_chat_job_sends_initial_task_and_followup_as_json_to_stdin(running_server, serve_module):
    """Full path: starting a ``chat`` job feeds the initial task as a JSON
    user message to the subprocess stdin, and a follow-up POST to
    ``/api/jobs/<id>/input`` is also JSON-wrapped (not sent as raw text).
    Stand-in subprocess just echoes every received stdin line back to
    stdout prefixed with ``GOT:`` so we can read them out of the log."""
    job_id = str(uuid.uuid4())
    echo_script = (
        "import sys\n"
        "for _ in range(2):\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    sys.stdout.write('GOT:' + line)\n"
        "    sys.stdout.flush()\n"
    )
    serve_module._start_subprocess_job(
        job_id=job_id,
        kind="chat",
        task="first turn",
        argv=[sys.executable, "-u", "-c", echo_script],
        initial_stdin=serve_module._chat_user_message("first turn"),
    )
    # Wait for subprocess to be alive so the followup actually reaches stdin.
    for _ in range(50):
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id].get("proc"):
                break
        time.sleep(0.05)

    # Follow-up turn via HTTP.
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/{job_id}/input",
        data=b'{"text": "second turn"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body

    # Wait for the echo subprocess to finish.
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id]["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)

    log = Path(serve_module.JOBS[job_id]["log_path"]).read_text(encoding="utf-8")
    got_lines = [ln for ln in log.splitlines() if ln.startswith("GOT:")]
    assert len(got_lines) == 2, f"expected 2 stdin lines, got log:\n{log}"
    first = json.loads(got_lines[0][len("GOT:"):])
    second = json.loads(got_lines[1][len("GOT:"):])
    assert first["type"] == "user"
    assert first["message"]["content"][0]["text"] == "first turn"
    assert second["type"] == "user"
    assert second["message"]["content"][0]["text"] == "second turn"


# ----- session resume (claude --resume <id>) -------------------------------


def test_build_chat_argv_uses_resume_flag_when_resuming(serve_module):
    """``_build_chat_argv(resume=True)`` swaps ``--session-id`` for
    ``--resume`` so claude continues the prior conversation instead of
    starting a fresh one with the same id (which is rejected)."""
    fresh = serve_module._build_chat_argv(model="m", session_id="abc", resume=False)
    resumed = serve_module._build_chat_argv(model="m", session_id="abc", resume=True)
    assert "--session-id" in fresh and "abc" in fresh
    assert "--resume" not in fresh
    assert "--resume" in resumed and "abc" in resumed
    assert "--session-id" not in resumed


def test_create_chat_job_with_resume_passes_resume_flag_to_argv(running_server, serve_module, monkeypatch):
    """POST /api/jobs ``{kind:"chat", task:"...", resume_session_id:"..."}``
    must spawn the subprocess with ``--resume <session_id>``. We capture the
    argv by stubbing :func:`subprocess.Popen` so the test doesn't shell out
    to the real ``claude`` binary."""
    captured = {}

    class _FakeProc:
        pid = 1234
        stdin = None
        stdout = None
        def wait(self): return 0

    real_popen = serve_module.subprocess.Popen

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        # Spawn a trivial harmless process so the runner thread finishes cleanly.
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)

    sid = "12345678-1234-1234-1234-1234567890ab"
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs",
        data=json.dumps({"kind": "chat", "task": "continue", "resume_session_id": sid}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 201, body
    # Give the runner thread a beat to actually call our fake Popen.
    for _ in range(40):
        if "argv" in captured:
            break
        time.sleep(0.05)
    assert "argv" in captured, "Popen was never called"
    assert "--resume" in captured["argv"], captured["argv"]
    assert sid in captured["argv"], captured["argv"]


def test_sessions_endpoint_lists_chat_jobs_with_session_id(running_server, serve_module):
    """``GET /api/sessions`` returns the list of chat-mode jobs and their
    session IDs so the operator can pick one to resume from the dashboard."""
    job_id = str(uuid.uuid4())
    sid = "abcd1234-abcd-1234-abcd-1234abcd1234"
    # Register a chat job manually (skip the real claude spawn) so we can
    # control the session_id without depending on Popen.
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id,
            "kind": "chat",
            "task": "first prompt",
            "status": "done",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:05+00:00",
            "exit_code": 0,
            "pid": None,
            "log_path": None,
            "session_id": sid,
            "model": "claude-sonnet-4-6",
        }
    status, body, _ = _http("GET", f"{running_server}/api/sessions")
    assert status == 200, body
    data = json.loads(body)
    assert "sessions" in data
    match = [s for s in data["sessions"] if s.get("session_id") == sid]
    assert len(match) == 1, data
    assert match[0]["task"] == "first prompt"
    assert match[0]["model"] == "claude-sonnet-4-6"


# ----- codex chat mode -----------------------------------------------------


def test_build_codex_chat_argv_for_initial_turn(serve_module):
    """First codex turn: ``codex exec --json -m <model>`` (prompt comes
    via stdin so we don't have to shell-quote it)."""
    argv = serve_module._build_codex_chat_argv(model="gpt-5.4", session_id=None)
    flags = argv[1:]
    assert flags[0] == "exec"
    assert "--json" in flags
    assert flags[flags.index("-m") + 1] == "gpt-5.4"
    assert "resume" not in flags  # initial turn is not a resume
    # Stdin is the prompt source; no PROMPT positional arg here.


def test_build_codex_chat_argv_for_resume_turn(serve_module):
    """Resume turn: ``codex exec resume <session_id>`` with prompt via stdin."""
    argv = serve_module._build_codex_chat_argv(model="gpt-5.4", session_id="cdx-1234")
    flags = argv[1:]
    assert flags[0] == "exec"
    assert "resume" in flags
    assert "cdx-1234" in flags
    assert "--json" in flags


def test_chat_codex_kind_is_an_allowed_job_kind(serve_module):
    """The ``chat-codex`` kind must be registered alongside ``chat`` so the
    HTTP layer accepts it without 400ing."""
    assert "chat-codex" in serve_module.JOB_KINDS


# ----- IDE transcript mirror (~/.claude/projects/<slug>/*.jsonl) ------------


def _make_fake_transcripts_dir(tmp_path, repo_cwd) -> Path:
    """Create a fake ~/.claude/projects layout with one transcript file."""
    projects = tmp_path / ".claude" / "projects"
    slug = str(repo_cwd).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    proj = projects / slug
    proj.mkdir(parents=True)
    sid = "12345678-1234-1234-1234-1234abcd0001"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi from IDE"}, "sessionId": sid, "cwd": str(repo_cwd)}) + "\n"
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi from claude"}]}, "sessionId": sid, "cwd": str(repo_cwd)}) + "\n",
        encoding="utf-8",
    )
    return projects


def test_list_transcripts_returns_session_files_for_current_repo(running_server, serve_module, tmp_path, monkeypatch):
    """``GET /api/transcripts`` returns the JSONL session files Claude Code
    has written for the current repo's working directory."""
    projects = _make_fake_transcripts_dir(tmp_path, serve_module.ROOT)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    status, body, _ = _http("GET", f"{running_server}/api/transcripts")
    assert status == 200, body
    data = json.loads(body)
    assert "transcripts" in data
    assert len(data["transcripts"]) >= 1
    sid = data["transcripts"][0]["session_id"]
    assert sid.endswith("abcd0001")
    assert data["transcripts"][0]["size_bytes"] > 0


def test_transcript_stream_emits_existing_lines_via_sse(running_server, serve_module, tmp_path, monkeypatch):
    """``GET /api/transcripts/<sid>/stream`` returns an SSE stream that
    flushes the file's existing JSONL content as ``data:`` frames so the
    operator immediately sees the IDE conversation."""
    projects = _make_fake_transcripts_dir(tmp_path, serve_module.ROOT)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    # Pull the session id back out of our fake file.
    sid = "12345678-1234-1234-1234-1234abcd0001"

    parsed = urlparse(f"{running_server}/api/transcripts/{sid}/stream")
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(
            f"GET /api/transcripts/{sid}/stream HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode("utf-8"),
        )
        sock.settimeout(3)
        buf = b""
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"hi from claude" in buf:
                break
    finally:
        sock.close()

    assert b"hi from IDE" in buf, buf[-400:]
    assert b"hi from claude" in buf, buf[-400:]


def test_transcript_stream_404_for_unknown_session(running_server, serve_module, tmp_path, monkeypatch):
    """Unknown session id -> 404 (don't leak filesystem state)."""
    projects = _make_fake_transcripts_dir(tmp_path, serve_module.ROOT)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    status, body, _ = _http(
        "GET",
        f"{running_server}/api/transcripts/00000000-0000-0000-0000-000000000000/stream",
    )
    assert status == 404, body


# ----- fork IDE session into a dashboard chat ------------------------------


def test_interrupt_endpoint_sends_control_request_to_chat_stdin(running_server, serve_module):
    """POST /api/jobs/<id>/interrupt writes a stream-json
    ``control_request`` envelope with ``subtype:"interrupt"`` to the
    subprocess stdin, so claude's running turn can be cancelled without
    killing the whole session."""
    job_id = str(uuid.uuid4())
    # Stand-in: read one line and echo it back.
    serve_module._start_subprocess_job(
        job_id=job_id,
        kind="chat",
        task="(noop)",
        argv=[sys.executable, "-u", "-c",
              "import sys; line = sys.stdin.readline(); sys.stdout.write('GOT:' + line); sys.stdout.flush()"],
    )
    for _ in range(50):
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id].get("proc"):
                break
        time.sleep(0.05)
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/{job_id}/interrupt",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id]["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)
    log = Path(serve_module.JOBS[job_id]["log_path"]).read_text(encoding="utf-8")
    got = [ln for ln in log.splitlines() if ln.startswith("GOT:")]
    assert got, "no stdin line captured; interrupt did not reach the subprocess"
    obj = json.loads(got[0][len("GOT:"):])
    assert obj["type"] == "control_request"
    assert obj["request"]["subtype"] == "interrupt"
    assert obj.get("request_id"), "control_request must carry a request_id"


def test_interrupt_endpoint_404_unknown_job(running_server):
    status, _, _ = _http("POST", f"{running_server}/api/jobs/00000000-0000-0000-0000-000000000000/interrupt",
                          data=b"{}", headers={"Content-Type": "application/json"})
    assert status == 404


def test_interrupt_endpoint_409_for_non_chat_job(running_server, serve_module):
    """Interrupt only makes sense for chat-kind jobs; orchestrate/plan
    runs have no control protocol so reject the request rather than
    silently writing JSON that confuses the subprocess."""
    job_id = _inject_python_job(serve_module, "import sys; sys.stdin.readline()\n")
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs/{job_id}/interrupt",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 409, body


def test_create_job_accepts_tags_and_summary_includes_them(running_server, serve_module, monkeypatch):
    """``POST /api/jobs`` accepts a ``tags`` array on creation and the
    returned summary echoes them back. Tags must be lowercase short
    slugs so the persistence ledger and resume picker can filter on them."""
    captured = {}

    class _FakeProc:
        pid = 9999
        stdin = None
        stdout = None
        def wait(self): return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        # Return a trivial real subprocess so the runner thread completes.
        return serve_module.subprocess.Popen.__wrapped__(
            [sys.executable, "-c", "pass"], **kwargs,
        ) if hasattr(serve_module.subprocess.Popen, "__wrapped__") else _FakeProc()

    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)

    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs",
        data=json.dumps({"kind": "chat", "task": "tagged", "tags": ["work", "auth"]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 201, body
    summary = json.loads(body)
    assert summary.get("tags") == ["work", "auth"], summary


def test_create_chat_job_permission_mode_replaces_dangerous_skip(running_server, serve_module, monkeypatch):
    """When ``permission_mode`` is provided on POST /api/jobs, the spawned
    claude argv uses ``--permission-mode <mode>`` instead of the blanket
    ``--dangerously-skip-permissions`` flag."""
    captured = {}

    real_popen = serve_module.subprocess.Popen

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)

    status, _, _ = _http(
        "POST",
        f"{running_server}/api/jobs",
        data=json.dumps({"kind": "chat", "task": "hi", "permission_mode": "acceptEdits"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 201
    for _ in range(40):
        if "argv" in captured:
            break
        time.sleep(0.05)
    argv = captured["argv"]
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "acceptEdits", argv
    assert "--dangerously-skip-permissions" not in argv, argv


def test_fork_session_argv_includes_fork_flag(serve_module):
    """``_build_chat_argv(resume=True, fork=True)`` keeps ``--resume <sid>``
    and adds ``--fork-session`` so claude branches off the existing
    transcript into a fresh session id without overwriting it."""
    argv = serve_module._build_chat_argv(
        model="m",
        session_id="sid-original",
        resume=True,
        fork=True,
    )
    assert "--resume" in argv and "sid-original" in argv
    assert "--fork-session" in argv


def test_create_job_with_fork_session_id_branches_existing_session(running_server, serve_module, monkeypatch):
    """POST /api/jobs ``{kind:"chat", task:"...", fork_session_id:"..."}``
    spawns claude with ``--resume <id> --fork-session`` so the resumed
    history is preserved but the new turns land in a fresh session."""
    captured = {}
    real_popen = serve_module.subprocess.Popen

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)
    sid = "12345678-1234-1234-1234-1234abcdfork"
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs",
        data=json.dumps({"kind": "chat", "task": "branch", "fork_session_id": sid}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 201, body
    for _ in range(40):
        if "argv" in captured:
            break
        time.sleep(0.05)
    argv = captured["argv"]
    assert "--resume" in argv and sid in argv
    assert "--fork-session" in argv


def test_create_job_rejects_invalid_tags(running_server):
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs",
        data=json.dumps({"kind": "chat", "task": "x", "tags": ["UPPER caseBAD!"]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 400, body


def test_resume_chat_job_sends_first_message_as_initial_stdin(running_server, serve_module, monkeypatch):
    """When the dashboard resumes an IDE/dashboard session via
    ``resume_session_id``, the operator's first typed message must be fed
    into the resumed ``claude --resume <sid>`` subprocess as a stream-json
    user envelope — otherwise the resumed claude just sits idle and the
    operator's input never reaches it."""
    captured: dict = {"stdin_writes": []}

    class _FakeStdin:
        def __init__(self): self.buf = b""
        def write(self, b): captured["stdin_writes"].append(bytes(b))
        def flush(self): pass
        closed = False

    class _FakeProc:
        pid = 4242
        def __init__(self):
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdoutEOF()
        def wait(self): return 0

    class _FakeStdoutEOF:
        def read(self, n): return b""

    real_popen = serve_module.subprocess.Popen

    def fake_popen(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        # Return our fake so the runner thread's stdin.write goes to us
        # instead of an actual child process.
        return _FakeProc()

    monkeypatch.setattr(serve_module.subprocess, "Popen", fake_popen)

    sid = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
    status, body, _ = _http(
        "POST",
        f"{running_server}/api/jobs",
        data=json.dumps({
            "kind": "chat",
            "task": "pick up where we left off please",
            "resume_session_id": sid,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 201, body

    # Give the runner thread a moment to call our fake stdin.write.
    for _ in range(50):
        if captured["stdin_writes"]:
            break
        time.sleep(0.05)

    assert "--resume" in captured["argv"], captured["argv"]
    assert captured["stdin_writes"], "no stdin write captured; resume isn't seeding the first user message"
    first_write = captured["stdin_writes"][0]
    obj = json.loads(first_write)
    assert obj["type"] == "user"
    assert obj["message"]["content"][0]["text"] == "pick up where we left off please"


# ----- persistence across server restart (jobs.jsonl) ----------------------


def test_persist_job_appends_snapshot_to_jsonl_file(tmp_path, serve_module, monkeypatch):
    """``_persist_job(job_id)`` appends the current snapshot of that job to
    ``JOBS_PERSIST_FILE`` as one JSON line. Runtime-only fields (proc,
    subscribers, stdin_lock) are stripped because they're not picklable
    and don't survive a restart anyway."""
    p = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(serve_module, "JOBS_PERSIST_FILE", p)
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id, "kind": "chat", "task": "remember this", "status": "queued",
            "created_at": "2026-01-01T00:00:00+00:00",
            "proc": object(),       # NOT serializable - must be skipped
            "subscribers": [object()],  # NOT serializable
            "stdin_lock": object(), # NOT serializable
        }
    serve_module._persist_job(job_id)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["id"] == job_id
    assert obj["task"] == "remember this"
    assert obj["status"] == "queued"
    assert "proc" not in obj
    assert "subscribers" not in obj
    assert "stdin_lock" not in obj


def test_persist_job_appends_one_line_per_call(tmp_path, serve_module, monkeypatch):
    """Each ``_persist_job`` call appends a new line so the on-disk log
    captures the full lifecycle (queued -> running -> done). Loading later
    keeps the LAST snapshot per job_id."""
    p = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(serve_module, "JOBS_PERSIST_FILE", p)
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {"id": job_id, "kind": "chat", "task": "x", "status": "queued", "created_at": "t"}
    serve_module._persist_job(job_id)
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id]["status"] = "running"
    serve_module._persist_job(job_id)
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id]["status"] = "done"
        serve_module.JOBS[job_id]["exit_code"] = 0
    serve_module._persist_job(job_id)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    statuses = [json.loads(l)["status"] for l in lines]
    assert statuses == ["queued", "running", "done"]


def test_load_persisted_jobs_rebuilds_JOBS_dict_with_last_snapshot(tmp_path, serve_module, monkeypatch):
    """``_load_persisted_jobs()`` reads the jsonl and rebuilds the JOBS
    dict, keeping the LAST snapshot for each job_id (so a queued -> done
    sequence ends up just as 'done')."""
    p = tmp_path / "jobs.jsonl"
    p.write_text("\n".join([
        json.dumps({"id": "a1", "kind": "chat", "status": "queued",  "task": "old",   "created_at": "2026-01-01"}),
        json.dumps({"id": "a1", "kind": "chat", "status": "running", "task": "old",   "created_at": "2026-01-01"}),
        json.dumps({"id": "a1", "kind": "chat", "status": "done",    "task": "old",   "created_at": "2026-01-01", "exit_code": 0}),
        json.dumps({"id": "a2", "kind": "chat", "status": "running", "task": "fresh", "created_at": "2026-01-02"}),
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(serve_module, "JOBS_PERSIST_FILE", p)

    with serve_module.JOBS_LOCK:
        # Clear any leakage from other tests.
        serve_module.JOBS.clear()
    serve_module._load_persisted_jobs()
    with serve_module.JOBS_LOCK:
        assert "a1" in serve_module.JOBS
        assert "a2" in serve_module.JOBS
        # a1 final snapshot is done.
        assert serve_module.JOBS["a1"]["status"] == "done"
        assert serve_module.JOBS["a1"]["exit_code"] == 0
        # a2 was running when serialized -> after restart we know the
        # subprocess is dead; mark it 'interrupted' so the UI is honest.
        assert serve_module.JOBS["a2"]["status"] == "interrupted"


def test_load_persisted_jobs_is_idempotent_when_file_missing(tmp_path, serve_module, monkeypatch):
    """Missing file -> no-op, no crash. (Fresh repo, first run.)"""
    p = tmp_path / "does-not-exist.jsonl"
    monkeypatch.setattr(serve_module, "JOBS_PERSIST_FILE", p)
    with serve_module.JOBS_LOCK:
        before = dict(serve_module.JOBS)
    serve_module._load_persisted_jobs()  # must not raise
    with serve_module.JOBS_LOCK:
        assert dict(serve_module.JOBS) == before


def test_persist_job_refuses_default_path_under_pytest(serve_module):
    """Defensive guard: under pytest, ``_persist_job`` must NOT write the
    real repo ledger. Tests that forget to monkeypatch ``JOBS_PERSIST_FILE``
    would otherwise pollute the developer's working ``.ai/ledgers/jobs.jsonl``
    with fixture entries (1800+ such pollution entries observed in the wild
    before this guard landed).

    The guard short-circuits when both conditions hold:
      - PYTEST_CURRENT_TEST env var set (pytest sets this per test)
      - JOBS_PERSIST_FILE still equals the module-default path (i.e. NOT
        monkeypatched to a tmp_path)
    """
    # No monkeypatch — JOBS_PERSIST_FILE points at the real repo path.
    assert serve_module.JOBS_PERSIST_FILE == serve_module._DEFAULT_JOBS_PERSIST_FILE
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id, "kind": "chat", "task": "should-not-land", "status": "queued",
        }
    size_before = (
        serve_module.JOBS_PERSIST_FILE.stat().st_size
        if serve_module.JOBS_PERSIST_FILE.exists() else 0
    )
    serve_module._persist_job(job_id)  # MUST be a no-op
    size_after = (
        serve_module.JOBS_PERSIST_FILE.stat().st_size
        if serve_module.JOBS_PERSIST_FILE.exists() else 0
    )
    assert size_after == size_before, (
        "_persist_job wrote to the real repo ledger from inside pytest — "
        "the PYTEST_CURRENT_TEST guard is broken"
    )
    # Clean up the JOBS dict so other tests don't see this stub.
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


def test_persist_job_writes_when_monkeypatched_to_tmp(tmp_path, serve_module, monkeypatch):
    """Counterpart to the guard test above: when JOBS_PERSIST_FILE IS
    monkeypatched to a tmp path, ``_persist_job`` writes normally even
    under pytest."""
    p = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(serve_module, "JOBS_PERSIST_FILE", p)
    job_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id, "kind": "chat", "task": "tmp-ok", "status": "done",
        }
    serve_module._persist_job(job_id)
    assert p.exists(), "monkeypatched path must be written"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["task"] == "tmp-ok"
    with serve_module.JOBS_LOCK:
        serve_module.JOBS.pop(job_id, None)


# ----- cost aggregation per chat job ---------------------------------------


def test_extract_cost_from_log_sums_all_result_turns(tmp_path, serve_module):
    """``_extract_cost_from_log(path)`` scans a chat-mode log for
    ``{"type":"result", ...}`` lines and sums their cost / duration /
    num_turns so the dashboard can show running totals per session."""
    log = tmp_path / "job.log"
    log.write_text("\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}',
        '{"type":"result","subtype":"end_turn","result":"hi","duration_ms":1200,"num_turns":1,"total_cost_usd":0.0023}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"again"}]}}',
        '{"type":"result","subtype":"end_turn","result":"again","duration_ms":800,"num_turns":2,"total_cost_usd":0.0017}',
    ]) + "\n", encoding="utf-8")

    cost = serve_module._extract_cost_from_log(log)
    assert cost is not None
    assert cost["turns"] == 2
    assert cost["cost_usd"] == pytest.approx(0.0040, rel=0.01)
    assert cost["duration_ms"] == 2000


def test_extract_cost_from_log_handles_missing_or_malformed(tmp_path, serve_module):
    """Empty file, missing file, junk lines -> returns None or zero-cost
    summary; never raises."""
    assert serve_module._extract_cost_from_log(tmp_path / "missing.log") is None

    log = tmp_path / "noise.log"
    log.write_text("not json\nstill not json\n", encoding="utf-8")
    summary = serve_module._extract_cost_from_log(log)
    assert summary is None or summary["turns"] == 0


def test_prune_old_logs_removes_files_past_threshold(tmp_path, serve_module, monkeypatch):
    """``_prune_old_logs(jobs_dir, max_age_days, keep_newest)`` deletes
    ``.log`` files older than the cutoff and caps the directory at
    ``keep_newest`` files. New files are preserved regardless of age."""
    import os
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    now = time.time()
    # Three "old" files (8d) and two "new" files (1h).
    old_files = []
    for i in range(3):
        f = jobs / f"old{i}.log"
        f.write_bytes(b"x")
        os.utime(f, (now - 8 * 86400, now - 8 * 86400))
        old_files.append(f)
    new_files = []
    for i in range(2):
        f = jobs / f"new{i}.log"
        f.write_bytes(b"y")
        new_files.append(f)

    deleted = serve_module._prune_old_logs(jobs, max_age_days=7, keep_newest=10)
    assert deleted == 3
    for f in old_files:
        assert not f.exists(), f
    for f in new_files:
        assert f.exists(), f


def test_prune_old_logs_keeps_only_newest_when_above_cap(tmp_path, serve_module):
    """When ``keep_newest`` is set and there are MORE recent files than
    the cap, the oldest beyond the cap are also pruned even if they're
    within the age limit."""
    import os
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    now = time.time()
    files = []
    for i in range(10):
        f = jobs / f"job{i}.log"
        f.write_bytes(b"x")
        os.utime(f, (now - i, now - i))  # i=0 newest, i=9 oldest
        files.append(f)
    serve_module._prune_old_logs(jobs, max_age_days=365, keep_newest=3)
    # The 3 newest remain (job0, job1, job2). job3..job9 deleted.
    survivors = sorted(p.name for p in jobs.glob("*.log"))
    assert survivors == ["job0.log", "job1.log", "job2.log"], survivors


def test_chat_job_log_path_points_at_transcript_file(tmp_path, serve_module, monkeypatch):
    """For chat jobs the dashboard reuses ``claude``'s own transcript file
    (which it writes anyway via ``--session-id``) instead of writing a
    redundant ``.log`` file under ``.ai/dashboard/jobs/``. ``log_path`` on
    the job record points at the transcript path so cost extraction and
    catch-up read from the single source of truth."""
    # Point the projects root at the tmp tree.
    projects = tmp_path / ".claude" / "projects"
    slug = str(serve_module.ROOT).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    (projects / slug).mkdir(parents=True)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    job_id = str(uuid.uuid4())
    sid = "11111111-aaaa-bbbb-cccc-222222222222"

    # Run a stand-in subprocess so we don't shell out to real claude.
    argv = [sys.executable, "-u", "-c", "import sys; sys.stdout.write('done'); sys.exit(0)"]

    # Pre-seed JOBS entry with kind=chat + session_id so the helper picks
    # the transcript path.
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id, "kind": "chat", "task": "test", "status": "queued",
            "created_at": "2026-01-01", "session_id": sid,
        }
    serve_module._start_subprocess_job(
        job_id=job_id, kind="chat", task="test", argv=argv,
    )
    # Wait for the runner to finish.
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id]["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)
    log_path = Path(serve_module.JOBS[job_id]["log_path"])
    # The chat job should not produce a file under .ai/dashboard/jobs.
    expected_legacy = serve_module.JOBS_DIR / f"{job_id}.log"
    assert not expected_legacy.exists(), f"redundant .log file appeared: {expected_legacy}"
    # log_path must point under the claude projects dir for this session.
    assert sid in str(log_path), log_path
    assert str(projects) in str(log_path), log_path


def test_chat_user_message_accepts_content_blocks_array(serve_module):
    """``_chat_user_message`` must support a content-block array (not just a
    plain string) so the dashboard composer can send text + image + inlined
    file blocks in a single user turn."""
    blocks = [
        {"type": "text", "text": "Look at this image:"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}},
        {"type": "text", "text": "and this file content:\n<file>hello</file>"},
    ]
    raw = serve_module._chat_user_message(blocks)
    assert raw.endswith(b"\n")
    obj = json.loads(raw)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"] == blocks


def test_skills_endpoint_lists_available_skills(running_server, serve_module):
    """``GET /api/skills`` returns slash-command names + descriptions parsed
    from local SKILL.md files (so the composer can autocomplete ``/orchestrate``)."""
    status, body, _ = _http("GET", f"{running_server}/api/skills")
    assert status == 200, body
    data = json.loads(body)
    assert "skills" in data
    # The repo ships at least the orchestrate + planner skills.
    names = {s["name"] for s in data["skills"]}
    assert "orchestrate" in names, names


def test_files_list_endpoint_returns_repo_files_for_autocomplete(running_server):
    """``GET /api/files/list?prefix=...`` returns repo-relative paths whose
    name matches the prefix — drives the ``@`` autocomplete picker."""
    status, body, _ = _http("GET", f"{running_server}/api/files/list?prefix=READ")
    assert status == 200, body
    data = json.loads(body)
    assert "files" in data
    assert any(p.lower().endswith("readme.md") for p in data["files"]), data["files"][:10]


def test_files_read_endpoint_returns_content_within_repo(running_server, serve_module):
    status, body, _ = _http("GET", f"{running_server}/api/files/read?path=README.md")
    assert status == 200, body
    data = json.loads(body)
    assert "content" in data
    assert len(data["content"]) > 0


def test_files_read_endpoint_rejects_path_outside_repo(running_server):
    """Path traversal must 403; the dashboard is a local tool but better
    safe than sorry."""
    status, body, _ = _http("GET", f"{running_server}/api/files/read?path=../../etc/passwd")
    assert status in (400, 403), body


def _spawn_echo_chat_job(serve_module, lines_to_read=4):
    """Inject a real ``chat``-kind subprocess that echoes each line of
    stdin back to stdout prefixed with ``GOT<n>:``. Returns (job_id,
    log_path). Used by tests that need to verify what reached stdin."""
    job_id = str(uuid.uuid4())
    script = (
        "import sys\n"
        f"for i in range({lines_to_read}):\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    sys.stdout.write(f'GOT{i}:' + line)\n"
        "    sys.stdout.flush()\n"
    )
    # Pre-seed JOBS so kind=chat is recognised by _send_to_stdin.
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id, "kind": "chat", "task": "x", "status": "queued",
            "created_at": "2026-01-01",
        }
    serve_module._start_subprocess_job(
        job_id=job_id, kind="chat", task="x",
        argv=[sys.executable, "-u", "-c", script],
    )
    # Wait until proc is alive AND status is running.
    for _ in range(50):
        with serve_module.JOBS_LOCK:
            j = serve_module.JOBS[job_id]
            if j.get("proc") and j.get("status") == "running":
                break
        time.sleep(0.05)
    return job_id


def test_input_endpoint_sends_image_block_to_subprocess_stdin(running_server, serve_module):
    """POSTing ``{text, images:[{data, media_type}]}`` to
    ``/api/jobs/<id>/input`` for a chat job results in a JSON envelope on
    stdin that contains both a text block and an image block."""
    job_id = _spawn_echo_chat_job(serve_module, lines_to_read=4)
    payload = {
        "text": "what is this picture?",
        "images": [{"data": "iVBORw0K", "media_type": "image/png"}],
    }
    status, body, _ = _http(
        "POST", f"{running_server}/api/jobs/{job_id}/input",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body

    # Wait for the echo to land in the log.
    log_path = Path(serve_module.JOBS[job_id]["log_path"])
    deadline = time.time() + 3
    while time.time() < deadline:
        if log_path.exists() and b"iVBORw0K" in log_path.read_bytes():
            break
        time.sleep(0.05)

    log = log_path.read_text(encoding="utf-8")
    got_lines = [ln for ln in log.splitlines() if ln.startswith("GOT")]
    image_line = next((ln for ln in got_lines if "iVBORw0K" in ln), None)
    assert image_line, f"image block didn't reach stdin; log was:\n{log}"
    obj = json.loads(image_line.split(":", 1)[1])
    assert obj["type"] == "user"
    content = obj["message"]["content"]
    types = [b.get("type") for b in content]
    assert "text" in types and "image" in types, content


def test_input_endpoint_inlines_referenced_files(running_server, serve_module):
    """POSTing ``{text, files:["README.md"]}`` for a chat job inlines the
    file contents into the user message so the agent sees them without
    the user pasting."""
    job_id = _spawn_echo_chat_job(serve_module, lines_to_read=4)
    payload = {"text": "what's in this file?", "files": ["README.md"]}
    status, body, _ = _http(
        "POST", f"{running_server}/api/jobs/{job_id}/input",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200, body

    log_path = Path(serve_module.JOBS[job_id]["log_path"])
    deadline = time.time() + 3
    while time.time() < deadline:
        if log_path.exists() and b"README.md" in log_path.read_bytes():
            break
        time.sleep(0.05)

    log = log_path.read_text(encoding="utf-8")
    got_lines = [ln for ln in log.splitlines() if ln.startswith("GOT") and "README.md" in ln]
    assert got_lines, f"file inlining didn't reach stdin; log:\n{log}"
    text = got_lines[-1]
    assert "README.md" in text
    assert "<file" in text or "```" in text  # any inlining wrapper


def test_chat_pump_tracks_cost_live_into_jobs_dict(serve_module):
    """The stdout pump for chat jobs parses ``type=result`` events as they
    stream in and aggregates cost into ``JOBS[id]['cost']`` so cost stays
    visible even when no local ``.log`` file is written."""
    job_id = str(uuid.uuid4())
    # Stand-in subprocess emits two result events spaced in time so we
    # exercise the accumulator path (not just one event).
    script = (
        "import sys, time\n"
        "sys.stdout.write('{\"type\":\"system\",\"subtype\":\"init\"}\\n')\n"
        "sys.stdout.flush(); time.sleep(0.05)\n"
        "sys.stdout.write('{\"type\":\"result\",\"subtype\":\"end_turn\",\"total_cost_usd\":0.0025,\"duration_ms\":700,\"num_turns\":1}\\n')\n"
        "sys.stdout.flush(); time.sleep(0.05)\n"
        "sys.stdout.write('{\"type\":\"result\",\"subtype\":\"end_turn\",\"total_cost_usd\":0.0015,\"duration_ms\":400,\"num_turns\":2}\\n')\n"
        "sys.stdout.flush()\n"
    )
    serve_module._start_subprocess_job(
        job_id=job_id, kind="chat", task="cost test",
        argv=[sys.executable, "-u", "-c", script],
    )
    deadline = time.time() + 3
    while time.time() < deadline:
        with serve_module.JOBS_LOCK:
            if serve_module.JOBS[job_id]["status"] in {"done", "failed"}:
                break
        time.sleep(0.05)
    with serve_module.JOBS_LOCK:
        cost = serve_module.JOBS[job_id].get("cost")
    assert cost is not None
    assert cost["turns"] == 2
    assert cost["cost_usd"] == pytest.approx(0.0040, rel=0.01)
    assert cost["duration_ms"] == 1100


def test_job_summary_includes_cost_for_chat_jobs(running_server, serve_module, tmp_path):
    """The ``/api/jobs/<id>`` summary surfaces the aggregated cost for chat
    jobs so the UI can show "$0.0040 · 2 turns" in the pane header."""
    job_id = str(uuid.uuid4())
    log_path = tmp_path / f"{job_id}.log"
    log_path.write_text(
        '{"type":"result","subtype":"end_turn","result":"x","duration_ms":1000,"num_turns":1,"total_cost_usd":0.005}\n',
        encoding="utf-8",
    )
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[job_id] = {
            "id": job_id, "kind": "chat", "task": "x", "status": "done",
            "created_at": "2026-01-01T00:00:00+00:00",
            "log_path": str(log_path), "session_id": "abc",
        }
    status, body, _ = _http("GET", f"{running_server}/api/jobs/{job_id}")
    assert status == 200, body
    data = json.loads(body)
    assert "cost" in data
    assert data["cost"]["cost_usd"] == pytest.approx(0.005, rel=0.01)
    assert data["cost"]["turns"] == 1


# ----- timeline aggregation (Gantt view) -----------------------------------


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_timeline_empty_events(tmp_path, serve_module, monkeypatch):
    """Missing or empty events.jsonl yields {runs: []} — no crash on a
    fresh repo where no phase has ever been dispatched."""
    p = tmp_path / "events.jsonl"
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    assert serve_module._load_timeline_runs() == []
    p.write_text("", encoding="utf-8")
    assert serve_module._load_timeline_runs() == []


def test_timeline_groups_by_session(tmp_path, serve_module, monkeypatch):
    """Events sharing a session_id collapse into one run; phases keep
    chronological order; distinct session_ids produce distinct runs."""
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "plan",    "tool": "claude", "model": "opus",    "exit_code": 0},
        {"ts": "2026-05-17T10:01:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "execute", "tool": "codex",  "model": "gpt-5.5", "exit_code": 0},
        {"ts": "2026-05-17T10:02:00Z", "kind": "phase_dispatch", "session_id": "s2",
         "phase": "plan",    "tool": "claude", "model": "opus",    "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    runs = serve_module._load_timeline_runs()
    assert len(runs) == 2
    by_sid = {r["session_id"]: r for r in runs}
    assert [ph["phase"] for ph in by_sid["s1"]["phases"]] == ["plan", "execute"]
    assert [ph["phase"] for ph in by_sid["s2"]["phases"]] == ["plan"]
    # Newest-first ordering by ended_at: s2 ended later (10:02 > 10:01).
    assert runs[0]["session_id"] == "s2"


def test_timeline_phase_duration_derived(tmp_path, serve_module, monkeypatch):
    """duration_ms of each phase = ts - prev_ts within the same session.
    The first phase in a run gets duration_ms = 0 (no prior timestamp)."""
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "plan",    "tool": "claude", "model": "opus",    "exit_code": 0},
        {"ts": "2026-05-17T10:00:30Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "execute", "tool": "codex",  "model": "gpt-5.5", "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    runs = serve_module._load_timeline_runs()
    phases = runs[0]["phases"]
    assert phases[0]["duration_ms"] == 0
    assert phases[1]["duration_ms"] == 30000


def test_timeline_status_classification(tmp_path, serve_module, monkeypatch):
    """status reflects exit_code: 0 → success, non-zero → failure,
    null/missing → pending."""
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "plan",    "tool": "claude", "model": "opus",    "exit_code": 0},
        {"ts": "2026-05-17T10:01:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "execute", "tool": "codex",  "model": "gpt-5.5", "exit_code": 1},
        {"ts": "2026-05-17T10:02:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "review",  "tool": "claude", "model": "opus",    "exit_code": None},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    runs = serve_module._load_timeline_runs()
    statuses = [ph["status"] for ph in runs[0]["phases"]]
    assert statuses == ["success", "failure", "pending"]


def test_timeline_includes_total_duration_and_tag(tmp_path, serve_module, monkeypatch):
    """Each run carries a `total_duration_ms` (ended_at - started_at) and
    a `tag` (the primary `tool/model` combo) so the row header can show
    'claude/opus-4-7 · 45s · 3 bars' without the frontend having to scan
    every phase."""
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "plan",    "tool": "claude", "model": "opus", "exit_code": 0},
        {"ts": "2026-05-17T10:00:45Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "execute", "tool": "claude", "model": "opus", "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", tmp_path / "no-transcripts")
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", tmp_path / "no-transcripts")
    runs = serve_module._load_timeline_runs()
    assert runs[0]["total_duration_ms"] == 45_000
    assert runs[0]["tag"] == "claude/opus"


def test_timeline_tag_mixed_when_multiple_tools(tmp_path, serve_module, monkeypatch):
    """If a single session dispatches phases through more than one
    tool/model combo, `tag` collapses to 'mixed' rather than picking one."""
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "plan",    "tool": "claude", "model": "opus",    "exit_code": 0},
        {"ts": "2026-05-17T10:00:30Z", "kind": "phase_dispatch", "session_id": "s1",
         "phase": "execute", "tool": "codex",  "model": "gpt-5.5", "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", tmp_path / "no-transcripts")
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", tmp_path / "no-transcripts")
    runs = serve_module._load_timeline_runs()
    assert runs[0]["tag"] == "mixed"


def test_timeline_task_from_transcript_when_available(tmp_path, serve_module, monkeypatch):
    """When a Claude transcript file exists for the run's session_id, the
    run's `task` field is populated with the first user message — so the
    timeline row can show what the session was actually about instead of
    just a UUID."""
    sid = "abcdef12-3456-7890-abcd-ef1234567890"
    # Fake projects root + transcripts dir for this repo's ROOT
    projects = tmp_path / ".claude" / "projects"
    cwd_str = str(serve_module.ROOT)
    slug = (cwd_str[0].lower() + cwd_str[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    tdir = projects / slug
    tdir.mkdir(parents=True)
    transcript = tdir / f"{sid}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user",
                    "message": {"role": "user", "content": "Add a timeline view to the dashboard"},
                    "sessionId": sid, "cwd": cwd_str}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": sid,
         "phase": "plan", "tool": "claude", "model": "opus", "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    runs = serve_module._load_timeline_runs()
    assert runs[0]["task"] == "Add a timeline view to the dashboard"


def test_timeline_task_skips_ide_injected_user_messages(tmp_path, serve_module, monkeypatch):
    """Claude Code wraps editor state (`<ide_opened_file>...`),
    `<system-reminder>`, command output, and tool results into transcript
    entries with `type: user`. Those aren't what the operator typed —
    the row task line must show the FIRST real prompt and skip these
    system-injected envelopes."""
    sid = "11112222-3333-4444-5555-666677778888"
    projects = tmp_path / ".claude" / "projects"
    cwd_str = str(serve_module.ROOT)
    slug = (cwd_str[0].lower() + cwd_str[1:]).replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    tdir = projects / slug
    tdir.mkdir(parents=True)
    transcript = tdir / f"{sid}.jsonl"
    transcript.write_text(
        "\n".join([
            json.dumps({"type": "user",
                        "message": {"role": "user",
                                    "content": "<ide_opened_file>The user opened the file foo.py</ide_opened_file>"},
                        "sessionId": sid}),
            json.dumps({"type": "user",
                        "message": {"role": "user",
                                    "content": "<system-reminder>be careful</system-reminder>"},
                        "sessionId": sid}),
            json.dumps({"type": "user",
                        "message": {"role": "user", "content": "fix the bug in the login flow"},
                        "sessionId": sid}),
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", projects)

    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": sid,
         "phase": "plan", "tool": "claude", "model": "opus", "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    runs = serve_module._load_timeline_runs()
    assert runs[0]["task"] == "fix the bug in the login flow"


def test_timeline_task_none_when_transcript_missing(tmp_path, serve_module, monkeypatch):
    """No transcript file → task is None (so the UI renders '(no transcript)'
    rather than crashing or surfacing a stale title)."""
    p = tmp_path / "events.jsonl"
    _write_events(p, [
        {"ts": "2026-05-17T10:00:00Z", "kind": "phase_dispatch", "session_id": "no-such-session",
         "phase": "plan", "tool": "claude", "model": "opus", "exit_code": 0},
    ])
    monkeypatch.setattr(serve_module, "EVENTS_FILE", p)
    monkeypatch.setattr(serve_module, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", tmp_path / "no-such-dir")
    monkeypatch.setattr(_tp, "_CLAUDE_PROJECTS_ROOT_OVERRIDE", tmp_path / "no-such-dir")
    runs = serve_module._load_timeline_runs()
    assert runs[0]["task"] is None


# ----- PID liveness reconciliation -----------------------------------------


def test_reconcile_marks_dead_pid_running_jobs_as_failed(serve_module):
    """`_reconcile_running_pids` flips any job whose status is
    running/queued/cancelling but whose tracked PID no longer exists into
    ``failed`` with a clear error. Otherwise the /api/jobs listing would
    show zombie "running" rows forever and the auto-open would keep
    surfacing dead chats."""
    alive_id = str(uuid.uuid4())
    dead_id = str(uuid.uuid4())
    # PID 1 always exists on POSIX (init); on Windows it's usually System.
    # PID 0xFFFFFFFE is a sentinel that won't exist on either platform.
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[alive_id] = {
            "id": alive_id, "kind": "chat", "task": "alive",
            "status": "running", "pid": os.getpid(),  # us — definitely alive
            "log_path": None, "exit_code": None,
            "created_at": None, "started_at": None, "ended_at": None,
        }
        serve_module.JOBS[dead_id] = {
            "id": dead_id, "kind": "chat", "task": "dead",
            "status": "running", "pid": 4294967294,  # 0xFFFFFFFE — does not exist
            "log_path": None, "exit_code": None,
            "created_at": None, "started_at": None, "ended_at": None,
        }
    try:
        serve_module._reconcile_running_pids()
        with serve_module.JOBS_LOCK:
            assert serve_module.JOBS[alive_id]["status"] == "running"
            assert serve_module.JOBS[dead_id]["status"] == "failed"
            assert "dead" in (serve_module.JOBS[dead_id].get("error") or "").lower() \
                   or "exit" in (serve_module.JOBS[dead_id].get("error") or "").lower()
    finally:
        with serve_module.JOBS_LOCK:
            serve_module.JOBS.pop(alive_id, None)
            serve_module.JOBS.pop(dead_id, None)


def test_jobs_list_endpoint_runs_reconcile_before_returning(running_server, serve_module):
    """``GET /api/jobs`` must reconcile dead PIDs before returning the
    list, so the frontend never sees a zombie running entry whose process
    has been gone for hours."""
    dead_id = str(uuid.uuid4())
    with serve_module.JOBS_LOCK:
        serve_module.JOBS[dead_id] = {
            "id": dead_id, "kind": "chat", "task": "zombie",
            "status": "running", "pid": 4294967294,
            "log_path": None, "exit_code": None,
            "created_at": "2024-01-01T00:00:00+00:00",
            "started_at": "2024-01-01T00:00:00+00:00",
            "ended_at": None,
        }
    try:
        status, body, _ = _http("GET", f"{running_server}/api/jobs")
        assert status == 200, body
        data = json.loads(body)
        matched = [j for j in data["jobs"] if j["id"] == dead_id]
        assert matched, "zombie job missing from response"
        assert matched[0]["status"] == "failed"
    finally:
        with serve_module.JOBS_LOCK:
            serve_module.JOBS.pop(dead_id, None)
