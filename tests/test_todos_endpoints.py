from __future__ import annotations

import importlib.util
import json
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

import server.runtime as _runtime  # BOUND_PORT + Origin allowlist live here (follows-the-move)


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture(scope="module")
def serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_serve_todos", SERVE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve_todos"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def running_server(tmp_path, monkeypatch, serve_module) -> Iterator[tuple[str, Path]]:
    (tmp_path / ".ai").mkdir()
    monkeypatch.setattr(serve_module, "ROOT", tmp_path)
    httpd = _ThreadingTCPServer(("127.0.0.1", 0), serve_module.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(serve_module, "PORT", port)
    monkeypatch.setattr(serve_module, "BOUND_PORT", port)
    # _origin_allowed reads BOUND_PORT from server.runtime's namespace now.
    monkeypatch.setattr(_runtime, "BOUND_PORT", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def _json_response(req: urllib.request.Request | str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(base: str, path: str) -> tuple[int, dict]:
    return _json_response(base + path)


def _post(base: str, path: str, body: dict, *, origin: bool = True) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if origin:
        parsed = urllib.parse.urlparse(base)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    req = urllib.request.Request(base + path, data=data, method="POST", headers=headers)
    return _json_response(req)


def _todo(todo_id: str, title: str, status: str, tags: list[str]) -> dict:
    return {
        "id": todo_id,
        "title": title,
        "tags": tags,
        "source": "test",
        "source_ref": "test",
        "status": status,
        "created_at": "2026-05-26T00:00:00Z",
        "updated_at": "2026-05-26T00:00:00Z",
        "captured_by": "test",
        "dedup_hash": todo_id,
        "resolution": None,
        "rejected_hashes": [],
    }


def _write_todos(repo_root: Path, rows: list[dict]) -> None:
    ledger = repo_root / ".ai" / "ledgers" / "todos.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    ledger.write_text(text, encoding="utf-8", newline="\n")


def test_get_todos_filters_by_status_and_tag(running_server):
    base, repo_root = running_server
    rows = [
        _todo("td_2026-05-26_001", "Fix endpoint test", "open", ["tests"]),
        _todo("td_2026-05-26_002", "Review docs", "open", ["docs"]),
        _todo("td_2026-05-26_003", "Resolve stale item", "resolved-suggested", ["tests"]),
    ]
    _write_todos(repo_root, rows)

    qs = urllib.parse.urlencode({"status": "open", "tag": "tests"})
    status, body = _get(base, f"/api/todos?{qs}")

    assert status == 200
    assert [todo["id"] for todo in body["todos"]] == ["td_2026-05-26_001"]
    assert body["counts"] == {
        "open": 2,
        "resolved-suggested": 1,
        "resolved": 0,
        "archived": 0,
    }
    assert body["banner"] is None


def test_post_todo_csrf_guard(running_server):
    base, _repo_root = running_server
    status, body = _post(base, "/api/todos", {"title": "Manual follow-up"}, origin=False)
    assert status == 403
    assert body["error"] == "origin not allowed"


def test_post_todo_validates_title_length(running_server):
    base, _repo_root = running_server
    status, body = _post(base, "/api/todos", {"title": "x" * 281})
    assert status == 400
    assert "title" in body["error"]


def test_post_todo_persists_description(running_server):
    base, _repo_root = running_server
    status, body = _post(
        base,
        "/api/todos",
        {"title": "Wire up export", "description": "First line.\nSecond line with detail."},
    )
    assert status == 201
    assert body["todo"]["description"] == "First line.\nSecond line with detail."

    # The created description survives the round trip through the list endpoint.
    status, listing = _get(base, "/api/todos")
    assert status == 200
    created = next(t for t in listing["todos"] if t["id"] == body["id"])
    assert created["description"] == "First line.\nSecond line with detail."


def test_post_todo_omitted_description_defaults_to_empty(running_server):
    base, _repo_root = running_server
    status, body = _post(base, "/api/todos", {"title": "No detail needed"})
    assert status == 201
    assert body["todo"]["description"] == ""


def test_post_todo_validates_description_length(running_server):
    base, _repo_root = running_server
    status, body = _post(
        base,
        "/api/todos",
        {"title": "Has overlong detail", "description": "y" * 2001},
    )
    assert status == 400
    assert "description" in body["error"]


def test_status_transitions_enforce_enum(running_server):
    base, repo_root = running_server
    todo_id = "td_2026-05-26_001"
    _write_todos(repo_root, [_todo(todo_id, "Close this item", "open", ["tests"])])

    status, body = _post(base, f"/api/todos/{todo_id}/status", {"action": "bogus"})
    assert status == 400
    assert body["error"] == "invalid action"

    status, body = _post(base, f"/api/todos/{todo_id}/status", {"action": "done"})
    assert status == 200
    assert body["todo"]["id"] == todo_id
    assert body["todo"]["status"] == "resolved"


def test_packet_schema_has_follow_ups_section():
    text = (REPO_ROOT / ".ai" / "packets" / "execute.md").read_text(encoding="utf-8")
    assert "\n## Follow-ups\n" in text
