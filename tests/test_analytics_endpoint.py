"""HTTP request tests for GET /api/analytics. Harness copied from
tests/test_agent_orchestrations_endpoint.py."""
from __future__ import annotations

import json
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

import pytest
import serve
import server.runtime  # BOUND_PORT + Origin allowlist now live here (follows-the-move)


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def running_server(monkeypatch, tmp_path) -> Iterator[str]:
    # VERIFIED constant names (JOBS_PERSIST_FILE / IMPROVEMENTS_LEDGER); no raising=False.
    for const, name in [("METRICS_FILE", "metrics.jsonl"),
                        ("SKILL_METRICS_FILE", "skill_metrics.jsonl"),
                        ("JOBS_PERSIST_FILE", "jobs.jsonl"), ("TODOS_FILE", "todos.jsonl"),
                        ("IMPROVEMENTS_LEDGER", "improvements.jsonl"),
                        ("EVENTS_FILE", "events.jsonl")]:
        p = tmp_path / name
        p.write_text("", encoding="utf-8")
        monkeypatch.setattr(serve, const, p)
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    httpd = _ThreadingTCPServer(("127.0.0.1", 0), serve.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(serve, "PORT", port)
    monkeypatch.setattr(serve, "BOUND_PORT", port)
    # _origin_allowed reads BOUND_PORT from server.runtime's namespace now.
    monkeypatch.setattr(server.runtime, "BOUND_PORT", port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)


# Copied verbatim from tests/test_agent_orchestrations_endpoint.py (lines 48-66).
def _origin(base: str) -> str:
    parsed = urllib.parse.urlparse(base)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_json(base, path, *, origin=None):
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


def test_analytics_endpoint_returns_documented_shape(running_server):
    status, body = _get_json(running_server, "/api/analytics?range=30d")
    assert status == 200
    assert body["range"] == "30d"
    for key in ("kpis", "cost", "health", "skills", "backlog"):
        assert key in body


def test_analytics_endpoint_defaults_bad_range(running_server):
    status, body = _get_json(running_server, "/api/analytics?range=bogus")
    assert status == 200
    assert body["range"] == "30d"
