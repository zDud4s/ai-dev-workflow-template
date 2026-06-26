"""Tests for the dashboard /api/council/* endpoints (Chunk 2).

Pattern mirrors tests/test_pipelines_endpoint.py — spin up an HTTPServer on an
OS-assigned port, point COUNCIL_RUNS_DIR (and the agents/models lookups) at
tmp_path, and exercise the live handler over urllib. The real subprocess spawn
is stubbed by monkeypatching serve._spawn_council_run so no claude/codex runs.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import pathlib
import socketserver
import sys
import threading
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / ".ai" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import serve  # noqa: E402


CATALOG = {
    "claude": ["claude-opus-4-8", "claude-sonnet-4-6"],
    "codex": ["gpt-5.5"],
}


# ---------------------------------------------------------------------------
# Shared monkeypatch helpers
# ---------------------------------------------------------------------------

def _patch_lookups(monkeypatch, tmp_path, *, agents=("security-reviewer",)) -> None:
    """Route COUNCIL_RUNS_DIR at tmp_path and stub the catalog/agent lookups so
    config + validation are deterministic regardless of the live repo state."""
    monkeypatch.setattr(serve, "COUNCIL_RUNS_DIR", tmp_path)
    monkeypatch.setattr(serve, "_read_models_catalog", lambda *a, **k: dict(CATALOG))
    monkeypatch.setattr(
        serve, "_scan_agents_dir",
        lambda *a, **k: [{"name": slug} for slug in agents],
    )


# ---------------------------------------------------------------------------
# Live-server fixture
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _live_server(tmp_path, monkeypatch, *, agents=("security-reviewer",)):
    _patch_lookups(monkeypatch, tmp_path, agents=agents)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), serve.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(serve, "PORT", port)
    monkeypatch.setattr(serve, "BOUND_PORT", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(port: int, path: str, *, origin: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body


def _post(port: int, path: str, body: dict, *, origin: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _stub_spawn(monkeypatch) -> list[dict]:
    """Replace serve._spawn_council_run with a no-op that records the run in the
    registry exactly like the real one (minus the subprocess) and returns the id.
    Returns the list of specs it was called with."""
    calls: list[dict] = []

    def fake_spawn(spec: dict) -> str:
        calls.append(spec)
        rid = spec["id"]
        serve.COUNCIL_RUNS[rid] = {
            "proc": None,
            "pid": None,
            "events": [],
            "subscribers": [],
            "status": "running",
        }
        # The real spawn also persists an initial record; mirror that so the
        # list/detail endpoints have something to read.
        (serve.COUNCIL_RUNS_DIR).mkdir(parents=True, exist_ok=True)
        (serve.COUNCIL_RUNS_DIR / f"{rid}.json").write_text(
            json.dumps({"id": rid, "question": spec["question"], "status": "running"}),
            encoding="utf-8",
        )
        return rid

    monkeypatch.setattr(serve, "_spawn_council_run", fake_spawn)
    return calls


VALID_BODY = {
    "question": "Why is the sky blue?",
    "seats": [
        {"type": "model", "ref": "claude-opus-4-8"},
        {"type": "model", "ref": "claude-sonnet-4-6"},
    ],
    "chairman": {"type": "model", "ref": "claude-opus-4-8"},
}


# ---------------------------------------------------------------------------
# GET /api/council/config
# ---------------------------------------------------------------------------

def test_config_payload_shape(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch, agents=("security-reviewer", "pr-strategist")) as port:
        status, body = _get(port, "/api/council/config")
    assert status == 200
    assert "default" in body
    assert "chairman" in body["default"] and "members" in body["default"]
    assert body["catalog"]["claude"] == CATALOG["claude"]
    assert body["catalog"]["codex"] == CATALOG["codex"]
    assert sorted(body["agents"]) == ["pr-strategist", "security-reviewer"]


# ---------------------------------------------------------------------------
# POST /api/council/runs — validation
# ---------------------------------------------------------------------------

def test_post_empty_question_400(tmp_path, monkeypatch) -> None:
    _stub_spawn(monkeypatch)
    with _live_server(tmp_path, monkeypatch) as port:
        bad = {**VALID_BODY, "question": "   "}
        status, body = _post(port, "/api/council/runs", bad)
    assert status == 400
    assert "question" in body.get("error", "").lower()


def test_post_no_members_400(tmp_path, monkeypatch) -> None:
    _stub_spawn(monkeypatch)
    with _live_server(tmp_path, monkeypatch) as port:
        bad = {**VALID_BODY, "seats": []}
        status, body = _post(port, "/api/council/runs", bad)
    assert status == 400


def test_post_unknown_ref_400(tmp_path, monkeypatch) -> None:
    _stub_spawn(monkeypatch)
    with _live_server(tmp_path, monkeypatch) as port:
        bad = {**VALID_BODY, "seats": [{"type": "model", "ref": "gpt-9-imaginary"}]}
        status, body = _post(port, "/api/council/runs", bad)
    assert status == 400
    assert "error" in body


def test_post_agent_on_codex_model_400(tmp_path, monkeypatch) -> None:
    _stub_spawn(monkeypatch)
    with _live_server(tmp_path, monkeypatch) as port:
        bad = {
            **VALID_BODY,
            "seats": [{"type": "agent", "ref": "security-reviewer", "model": "gpt-5.5"}],
        }
        status, body = _post(port, "/api/council/runs", bad)
    assert status == 400


def test_post_cross_origin_403(tmp_path, monkeypatch) -> None:
    _stub_spawn(monkeypatch)
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _post(port, "/api/council/runs", VALID_BODY, origin="http://evil.example")
    assert status == 403


def test_post_valid_returns_id_and_registers(tmp_path, monkeypatch) -> None:
    calls = _stub_spawn(monkeypatch)
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _post(port, "/api/council/runs", VALID_BODY)
    assert status == 200, body
    rid = body["id"]
    assert rid and rid in serve.COUNCIL_RUNS
    assert (tmp_path / f"{rid}.json").is_file()
    assert calls and calls[0]["question"] == VALID_BODY["question"]
    serve.COUNCIL_RUNS.pop(rid, None)


# ---------------------------------------------------------------------------
# GET /api/council/runs  +  GET /api/council/runs/<id>
# ---------------------------------------------------------------------------

def test_list_runs_newest_first_excludes_gitkeep(tmp_path, monkeypatch) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "20260101-000000-aaaaaa.json").write_text(
        json.dumps({"id": "20260101-000000-aaaaaa", "question": "old"}), encoding="utf-8")
    (tmp_path / "20260202-000000-bbbbbb.json").write_text(
        json.dumps({"id": "20260202-000000-bbbbbb", "question": "new"}), encoding="utf-8")
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _get(port, "/api/council/runs")
    assert status == 200
    ids = [r["id"] for r in body["runs"]]
    assert ids == ["20260202-000000-bbbbbb", "20260101-000000-aaaaaa"]


def test_detail_returns_record(tmp_path, monkeypatch) -> None:
    rid = "20260202-000000-bbbbbb"
    (tmp_path / f"{rid}.json").write_text(
        json.dumps({"id": rid, "question": "new", "status": "done"}), encoding="utf-8")
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _get(port, f"/api/council/runs/{rid}")
    assert status == 200
    assert body["id"] == rid
    assert body["status"] == "done"


def test_detail_unknown_404(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _get(port, "/api/council/runs/20990101-000000-zzzzzz")
    assert status == 404


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

def test_stream_cross_origin_blocked(tmp_path, monkeypatch) -> None:
    rid = "20260202-000000-bbbbbb"
    serve.COUNCIL_RUNS[rid] = {
        "proc": None, "pid": None, "events": [], "subscribers": [], "status": "running",
    }
    try:
        with _live_server(tmp_path, monkeypatch) as port:
            status, _ = _get(port, f"/api/council/runs/{rid}/stream", origin="http://evil.example")
        assert status == 403
    finally:
        serve.COUNCIL_RUNS.pop(rid, None)


def test_stream_replays_buffered_events(tmp_path, monkeypatch) -> None:
    """Catch-up replays the in-memory events buffer, then closes when the run is
    already terminal (no live subprocess)."""
    rid = "20260202-000000-cccccc"
    serve.COUNCIL_RUNS[rid] = {
        "proc": None,
        "pid": None,
        "events": [
            {"stage": 1, "seat_idx": 0, "status": "ok", "field": "response", "value": "hi"},
            {"stage": "run", "status": "done"},
        ],
        "subscribers": [],
        "status": "done",
    }
    try:
        with _live_server(tmp_path, monkeypatch) as port:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/council/runs/{rid}/stream"
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                raw = r.read().decode("utf-8")
        # Both buffered events should appear in the catch-up replay.
        assert '"seat_idx": 0' in raw or '"seat_idx":0' in raw
        assert '"stage": "run"' in raw or '"stage":"run"' in raw
    finally:
        serve.COUNCIL_RUNS.pop(rid, None)


# ---------------------------------------------------------------------------
# POST /api/council/runs/<id>/cancel
# ---------------------------------------------------------------------------

def test_cancel_unknown_404(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _post(port, "/api/council/runs/20990101-000000-zzzzzz/cancel", {})
    assert status == 404


def test_cancel_marks_cancelled(tmp_path, monkeypatch) -> None:
    rid = "20260202-000000-dddddd"

    class _FakeProc:
        # Real subprocess.Popen always has a .pid; mirror that. pid=None here
        # routes _cancel_council_run to the proc.terminate() fallback path.
        pid = None

        def __init__(self):
            self.terminated = False

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True

    proc = _FakeProc()
    serve.COUNCIL_RUNS[rid] = {
        "proc": proc, "pid": None, "events": [], "subscribers": [], "status": "running",
    }
    (tmp_path / f"{rid}.json").write_text(
        json.dumps({"id": rid, "question": "q", "status": "running"}), encoding="utf-8")
    try:
        with _live_server(tmp_path, monkeypatch) as port:
            status, body = _post(port, f"/api/council/runs/{rid}/cancel", {})
        assert status == 200, body
        assert serve.COUNCIL_RUNS[rid]["status"] == "cancelled"
        record = json.loads((tmp_path / f"{rid}.json").read_text(encoding="utf-8"))
        assert record["status"] == "cancelled"
    finally:
        serve.COUNCIL_RUNS.pop(rid, None)
