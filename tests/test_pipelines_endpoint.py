"""Tests for the dashboard pipeline endpoints.

Pattern mirrors tests/test_agent_orchestrations_endpoint.py — spin up an
HTTPServer on an OS-assigned port, point AGENT-style constants at tmp_path,
and exercise the live handler over urllib.
"""
from __future__ import annotations
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / ".ai" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import serve  # noqa: E402
import server.runtime  # noqa: E402 — BOUND_PORT + Origin allowlist now live here (follows-the-move)
import server.pipelines as _pl  # _list_pipelines reads PIPELINES_DIR here (follows-the-move)
import server.handlers.pipelines as _plh  # noqa: E402 — pipeline GET/PUT/DELETE handlers read PIPELINES_DIR here


def _write_pipeline(d: pathlib.Path, slug: str, yaml_body: str) -> pathlib.Path:
    f = d / f"{slug}.yaml"
    f.write_text(yaml_body, encoding="utf-8")
    return f


VALID_YAML = """description: Quick chain
nodes:
  - id: input
    kind: input
  - id: explore
    agent: code-explorer
    depends_on: [input]
  - id: review
    agent: code-architect
    depends_on: [explore]
  - id: out
    kind: passthrough
    depends_on: [review]
"""


# ---------------------------------------------------------------------------
# Helper-level tests (no live server yet)
# ---------------------------------------------------------------------------

def test_list_pipelines_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(serve, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(_pl, "PIPELINES_DIR", tmp_path)  # follows-the-move
    assert serve._list_pipelines() == []


def test_list_pipelines_one_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(serve, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(_pl, "PIPELINES_DIR", tmp_path)  # follows-the-move
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    rows = serve._list_pipelines()
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "demo"
    assert row["node_count"] == 2          # agent nodes only (explore, review)
    assert row["output_mode"] == "passthrough"  # from the sink node's kind
    assert "shape" not in row              # shape badge removed


def test_list_pipelines_excludes_gitkeep(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(serve, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(_pl, "PIPELINES_DIR", tmp_path)  # follows-the-move
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    rows = serve._list_pipelines()
    assert [r["slug"] for r in rows] == ["demo"]


# ---------------------------------------------------------------------------
# Live-server fixture (port-binding + origin/CSRF semantics)
# ---------------------------------------------------------------------------

import contextlib
import http.server
import socketserver

@contextlib.contextmanager
def _live_server(tmp_path, monkeypatch):
    """Start serve.Handler bound to a random localhost port; route PIPELINES_DIR
    at the supplied tmp_path. Mirror test_agent_orchestrations_endpoint's pattern.
    """
    monkeypatch.setattr(serve, "PIPELINES_DIR", tmp_path)
    monkeypatch.setattr(_pl, "PIPELINES_DIR", tmp_path)  # follows-the-move
    monkeypatch.setattr(_plh, "PIPELINES_DIR", tmp_path)  # GET/PUT/DELETE handlers read it here
    httpd = socketserver.TCPServer(("127.0.0.1", 0), serve.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(serve, "PORT", port)
    monkeypatch.setattr(serve, "BOUND_PORT", port)
    # _origin_allowed reads BOUND_PORT from server.runtime's namespace now.
    monkeypatch.setattr(server.runtime, "BOUND_PORT", port)
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
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body


# ---------------------------------------------------------------------------
# GET /api/pipelines
# ---------------------------------------------------------------------------

def test_get_list_returns_empty(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _get(port, "/api/pipelines")
        assert status == 200
        assert body == {"pipelines": []}


def test_get_list_returns_one_pipeline(tmp_path, monkeypatch) -> None:
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _get(port, "/api/pipelines")
        assert status == 200
        assert len(body["pipelines"]) == 1
        assert body["pipelines"][0]["slug"] == "demo"


# ---------------------------------------------------------------------------
# GET /api/pipelines/<slug>
# ---------------------------------------------------------------------------

def test_get_pipeline_detail(tmp_path, monkeypatch) -> None:
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _get(port, "/api/pipelines/demo")
        assert status == 200
        assert body["slug"] == "demo"
        assert body["description"] == "Quick chain"
        # output is now structural - the sink node carries the kind
        sink = next(n for n in body["nodes"] if n.get("kind") in ("synthesize", "collect", "passthrough"))
        assert sink["kind"] == "passthrough"
        assert len(body["nodes"]) == 4


def test_get_pipeline_invalid_slug(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _get(port, "/api/pipelines/INVALID%20SLUG")
        assert status == 400
        assert "slug" in body.get("error", "").lower()


def test_get_pipeline_path_traversal(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _get(port, "/api/pipelines/..%2F..%2Fetc%2Fpasswd")
        assert status in (400, 404)  # rejected one way or the other


def test_get_pipeline_unknown_slug(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _get(port, "/api/pipelines/ghost")
        assert status == 404


def test_get_cross_origin_blocked(tmp_path, monkeypatch) -> None:
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _get(port, "/api/pipelines", origin="http://evil.example")
        assert status == 403


# ---------------------------------------------------------------------------
# PUT / DELETE helpers (CSRF-guarded — _csrf_guard internally requires
# a same-origin Origin header pointing at the bound port.)
# ---------------------------------------------------------------------------

def _put(port: int, path: str, body: dict, *, origin: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="PUT",
    )
    req.add_header("Content-Type", "application/json")
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _delete(port: int, path: str, *, origin: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    if origin is None:
        origin = f"http://127.0.0.1:{port}"
    req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def test_put_valid_pipeline_creates_file(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _put(port, "/api/pipelines/demo", {"yaml": VALID_YAML})
        assert status == 200, body
        assert (tmp_path / "demo.yaml").is_file()


def test_put_invalid_yaml_400(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _put(port, "/api/pipelines/demo", {"yaml": "::: not yaml"})
        assert status == 400


def test_put_schema_violation_400(tmp_path, monkeypatch) -> None:
    bad = "nodes:\n  - id: a\n    agent: x\n# missing input/sink nodes\n"
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _put(port, "/api/pipelines/demo", {"yaml": bad})
        assert status == 400
        assert "errors" in body


def test_put_cross_origin_403(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _put(port, "/api/pipelines/demo", {"yaml": VALID_YAML}, origin="http://evil.example")
        assert status == 403


def test_put_invalid_slug_400(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, body = _put(port, "/api/pipelines/BAD%20SLUG", {"yaml": VALID_YAML})
        assert status == 400


def test_put_oversized_body_400(tmp_path, monkeypatch) -> None:
    big = "x" * (260 * 1024)
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _put(port, "/api/pipelines/demo", {"yaml": big})
        assert status == 400


def test_delete_existing_pipeline(tmp_path, monkeypatch) -> None:
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _delete(port, "/api/pipelines/demo")
        assert status == 200
        assert not (tmp_path / "demo.yaml").is_file()


def test_delete_unknown_404(tmp_path, monkeypatch) -> None:
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _delete(port, "/api/pipelines/ghost")
        assert status == 404


def test_delete_cross_origin_403(tmp_path, monkeypatch) -> None:
    _write_pipeline(tmp_path, "demo", VALID_YAML)
    with _live_server(tmp_path, monkeypatch) as port:
        status, _ = _delete(port, "/api/pipelines/demo", origin="http://evil.example")
        assert status == 403
