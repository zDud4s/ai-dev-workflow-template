"""Unit + integration tests for the Agent Suggestions panel.

Covers the helper layer (parse / persist / catalog) plus the four HTTP
endpoints (POST /api/agents/suggest, GET /api/agents/proposals, GET
/api/agents/proposals/<id>, POST /api/agents/proposals/<id>/(accept|reject)).

The suggester endpoint normally spawns `claude -p` to draft suggestions —
that is replaced here by monkeypatching `subprocess.run` to return a canned
JSON response. No real LLM is invoked.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse
from http.client import HTTPResponse
from pathlib import Path

import pytest

import server.runtime as _runtime  # BOUND_PORT + Origin allowlist live here (follows-the-move)


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


@pytest.fixture(scope="module")
def serve_module():
    """Load `.ai/dashboard/serve.py` as a module without running main()."""
    spec = importlib.util.spec_from_file_location("dashboard_serve_agentsugg", SERVE_PATH)
    assert spec and spec.loader, "could not load serve.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_agentsugg"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def isolated_proposals_dir(tmp_path, monkeypatch, serve_module):
    """Redirect AGENT_PROPOSALS_DIR to a per-test tmp dir.

    Without this every persist test writes into the real
    `.ai/dashboard/proposals/agents/` and proposals leak between runs.
    """
    d = tmp_path / "agent_proposals"
    monkeypatch.setattr(serve_module, "AGENT_PROPOSALS_DIR", d)
    return d


@pytest.fixture
def isolated_agents_dir(tmp_path, monkeypatch, serve_module):
    """Redirect ROOT so Accept writes into a per-test tmp .claude/agents/.

    The handler resolves the target as `ROOT/.claude/agents/<slug>.md`, so
    pointing ROOT at a fresh tmp_path is enough to keep the test off the
    real repo. AGENT_PROPOSALS_DIR is rebound separately via the other
    fixture (its module-level default reads ROOT at import time, so a
    bare ROOT swap leaves it pointing at the real repo)."""
    monkeypatch.setattr(serve_module, "ROOT", tmp_path)
    return tmp_path / ".claude" / "agents"


@pytest.fixture
def running_server(serve_module):
    """Start the dashboard HTTP server on an ephemeral port.

    Monkeypatches PORT + BOUND_PORT so _origin_allowed accepts the
    ephemeral port (the allowlist keys on BOUND_PORT).
    """
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


def _http(method: str, url: str, data: bytes | None = None,
          headers: dict | None = None) -> tuple[int, bytes, dict]:
    # Auto-inject a same-origin Origin header on mutating requests so the
    # CSRF guard accepts them. Callers can override by passing Origin.
    merged = dict(headers or {})
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and "Origin" not in merged:
        parsed = urlparse(url)
        merged["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    req = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # type: HTTPResponse
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


# ----- helper-layer tests ---------------------------------------------------


def test_parse_agent_suggestions_valid(serve_module):
    payload = json.dumps({
        "suggestions": [
            {
                "name": "build-doctor",
                "description": "Diagnose flaky builds",
                "trigger_phrasings": ["why did the build fail", "fix the build"],
                "rationale": "Recurring CI failures keep eating time",
                "tools": "Bash, Read, Grep",
                "confidence": "high",
                "body": "Purpose: investigate failed builds.\n\n- step 1\n- step 2",
            },
            {
                "name": "release-notes",
                "description": "Draft release notes",
                "trigger_phrasings": ["write release notes"],
                "rationale": "Multiple manual notes drafts in git log",
                "tools": "",
                "confidence": "medium",
                "body": "Purpose: draft release notes.\n\n- step 1",
            },
        ]
    })
    out = serve_module._parse_agent_suggestions_output(payload)
    assert isinstance(out, list)
    assert len(out) == 2
    assert {s["slug"] for s in out} == {"build-doctor", "release-notes"}
    assert out[0]["confidence"] == "high"


def test_parse_agent_suggestions_invalid(serve_module):
    assert serve_module._parse_agent_suggestions_output("not json at all") is None
    # Object without "suggestions" list:
    assert serve_module._parse_agent_suggestions_output('{"foo": 1}') is None
    # "suggestions" present but wrong shape:
    assert serve_module._parse_agent_suggestions_output('{"suggestions": "x"}') is None


def test_parse_agent_suggestions_drops_bad_items(serve_module):
    """Mix of valid + invalid items returns only the valid subset."""
    payload = json.dumps({
        "suggestions": [
            {"name": "", "description": "no name", "trigger_phrasings": [],
             "tools": "", "confidence": "low", "body": "x"},
            {"name": "ok", "description": "fine", "trigger_phrasings": ["t"],
             "tools": "", "confidence": "high", "body": "Purpose: x.\n\n- s"},
            {"name": "no-body", "description": "missing body",
             "trigger_phrasings": ["t"], "tools": "", "confidence": "low"},
        ]
    })
    out = serve_module._parse_agent_suggestions_output(payload)
    assert out is not None
    assert [s["slug"] for s in out] == ["ok"]


def test_persist_writes_json_and_body(serve_module, isolated_proposals_dir):
    suggestion = {
        "name": "test-agent",
        "slug": "test-agent",
        "description": "desc",
        "trigger_phrasings": ["a", "b"],
        "rationale": "why",
        "tools": "Read",
        "confidence": "high",
        "body": "Purpose: x.\n\n- step",
    }
    pid = serve_module._persist_agent_proposal(suggestion, source_signal={"jobs": 0})
    assert pid and pid.startswith("_agent-test-agent-")
    j = (isolated_proposals_dir / f"{pid}.json").read_text(encoding="utf-8")
    body = (isolated_proposals_dir / f"{pid}.body.md").read_text(encoding="utf-8")
    obj = json.loads(j)
    assert obj["slug"] == "test-agent"
    assert obj["status"] == "pending"
    assert obj["target_path"] == ".claude/agents/test-agent.md"
    assert "Purpose: x." in body


def test_load_editable_agent_names_smoke(serve_module, tmp_path, monkeypatch):
    """Pointing both candidate dirs at tmp dirs gives back exactly the
    filenames we put there. We monkeypatch Path.home() to keep the user
    scope sandboxed too."""
    proj_dir = tmp_path / "repo" / ".claude" / "agents"
    user_dir = tmp_path / "user_home" / ".claude" / "agents"
    proj_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)
    (proj_dir / "foo.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
    (proj_dir / "Bar.md").write_text("---\nname: Bar\n---\n", encoding="utf-8")
    (user_dir / "baz.md").write_text("---\nname: baz\n---\n", encoding="utf-8")
    monkeypatch.setattr(serve_module, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(serve_module.Path, "home", lambda: tmp_path / "user_home")
    names = serve_module._load_editable_agent_names()
    assert names == {"foo", "bar", "baz"}


# ----- endpoint-level tests -------------------------------------------------


def test_proposals_list_returns_pending(serve_module, isolated_proposals_dir, running_server):
    """Two persisted proposals show up in the list endpoint, ordered by
    mtime desc (newest first)."""
    s = {
        "name": "alpha", "slug": "alpha",
        "description": "first", "trigger_phrasings": ["a"],
        "rationale": "r", "tools": "", "confidence": "high",
        "body": "Purpose: x.\n",
    }
    pid1 = serve_module._persist_agent_proposal(s, source_signal={})
    s2 = dict(s, name="beta", slug="beta", description="second")
    pid2 = serve_module._persist_agent_proposal(s2, source_signal={})
    status, body, _ = _http("GET", f"{running_server}/api/agents/proposals")
    assert status == 200, body
    data = json.loads(body)
    assert len(data["proposals"]) == 2
    ids = {p["id"] for p in data["proposals"]}
    assert ids == {pid1, pid2}


def test_accept_writes_agent_file_and_refuses_overwrite(serve_module, isolated_proposals_dir, isolated_agents_dir, running_server):
    """Accept materialises the agent file. A second Accept on the same
    slug fails with 409 because the file already exists."""
    suggestion = {
        "name": "new-thing", "slug": "new-thing",
        "description": "creates new thing", "trigger_phrasings": ["make a thing"],
        "rationale": "users keep asking", "tools": "Read, Edit",
        "confidence": "high", "body": "Purpose: build a thing.\n\n- step",
    }
    pid = serve_module._persist_agent_proposal(suggestion, source_signal={})
    status, body, _ = _http(
        "POST", f"{running_server}/api/agents/proposals/{pid}/accept")
    assert status == 200, body
    target = isolated_agents_dir / "new-thing.md"
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert content.startswith("---\nname: new-thing\n")
    assert "description: creates new thing" in content
    assert "tools: Read, Edit" in content
    assert "Purpose: build a thing." in content

    # Reload the proposal (now status=installed) and try to re-accept by
    # writing a second proposal for the same slug — that one should 409
    # because the agent file already exists.
    pid2 = serve_module._persist_agent_proposal(suggestion, source_signal={})
    status, body, _ = _http(
        "POST", f"{running_server}/api/agents/proposals/{pid2}/accept")
    assert status == 409, body
    err = json.loads(body)
    assert "agent already exists" in (err.get("error") or "")


def test_reject_marks_status(serve_module, isolated_proposals_dir, isolated_agents_dir, running_server):
    suggestion = {
        "name": "skip-me", "slug": "skip-me",
        "description": "no thanks", "trigger_phrasings": [],
        "rationale": "", "tools": "", "confidence": "low",
        "body": "Purpose: not interesting.\n",
    }
    pid = serve_module._persist_agent_proposal(suggestion, source_signal={})
    status, body, _ = _http(
        "POST", f"{running_server}/api/agents/proposals/{pid}/reject")
    assert status == 200, body
    obj = json.loads((isolated_proposals_dir / f"{pid}.json").read_text(encoding="utf-8"))
    assert obj["status"] == "rejected"
    # File must NOT be created on reject.
    assert not (isolated_agents_dir / "skip-me.md").exists()


def test_proposal_id_validator_rejects_malformed(serve_module, running_server):
    """Defence in depth: the route regex only accepts `[A-Za-z0-9_\\-]+`, so
    a URL with `%2F` or `..` chars falls through to a 404 from the base
    file-server. IDs that DO pass the router but don't match the handler's
    strict id format (`_agent-<slug>-YYYYMMDD-HHMMSS`) get 400."""
    # URL chars outside [A-Za-z0-9_-] don't match the router → 404 (still safe).
    status, _, _ = _http("GET", f"{running_server}/api/agents/proposals/..%2Fetc%2Fpasswd")
    assert status == 404
    # Well-formed URL but invalid id → router matches, validator rejects with 400.
    status, _, _ = _http("GET", f"{running_server}/api/agents/proposals/notvalid")
    assert status == 400
    status, _, _ = _http("POST", f"{running_server}/api/agents/proposals/notvalid/accept")
    assert status == 400


def test_suggest_endpoint_persists_proposals(serve_module, isolated_proposals_dir, isolated_agents_dir, running_server, monkeypatch):
    """End-to-end POST /api/agents/suggest with subprocess.run mocked.

    Validates the full pipeline: subprocess call → parse → persist → return
    `{count, proposal_ids}`. The mocked subprocess returns a JSON blob with
    two valid suggestions.
    """
    import subprocess as _sp

    canned = json.dumps({"suggestions": [
        {"name": "alpha", "description": "first thing",
         "trigger_phrasings": ["run alpha"], "rationale": "repeated",
         "tools": "", "confidence": "high",
         "body": "Purpose: alpha.\n\n- step"},
        {"name": "beta", "description": "second thing",
         "trigger_phrasings": ["run beta"], "rationale": "repeated too",
         "tools": "Read", "confidence": "medium",
         "body": "Purpose: beta.\n\n- step"},
    ]})

    class FakeProc:
        returncode = 0
        stdout = canned
        stderr = ""

    def fake_run(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(serve_module.subprocess, "run", fake_run)
    # Pretend the CLI is on PATH so the early 503 doesn't fire.
    # `_safe_which` calls `shutil.which(name, path=cleaned)`, so the stub
    # needs to accept the keyword argument.
    monkeypatch.setattr(serve_module.shutil, "which", lambda _name, path=None: "/fake/claude")
    # Skip transcript-purge side effect.
    monkeypatch.setattr(serve_module, "_purge_claude_transcript", lambda *_a, **_k: None)

    status, body, _ = _http("POST", f"{running_server}/api/agents/suggest")
    assert status == 200, body
    data = json.loads(body)
    assert data["count"] == 2
    assert len(data["proposal_ids"]) == 2
    files = sorted(isolated_proposals_dir.glob("*.json"))
    assert len(files) == 2
