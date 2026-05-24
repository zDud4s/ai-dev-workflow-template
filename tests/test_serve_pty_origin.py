import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
import serve

import pytest


class FakeHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.errors = []
        self.responses = []

    def send_error(self, code, msg=None):
        self.errors.append((code, msg))

    def _json(self, code, payload):
        self.responses.append((code, payload))


def test_origin_absent_rejected():
    assert serve._origin_allowed({}) is False


def test_origin_loopback_ipv4_allowed():
    assert serve._origin_allowed({"Origin": f"http://127.0.0.1:{serve.PORT}"}) is True


def test_origin_localhost_allowed():
    assert serve._origin_allowed({"Origin": f"http://localhost:{serve.PORT}"}) is True


def test_origin_ipv6_loopback_allowed():
    assert serve._origin_allowed({"Origin": f"http://[::1]:{serve.PORT}"}) is True


def test_origin_cross_site_rejected():
    assert serve._origin_allowed({"Origin": "http://evil.example.com"}) is False


def test_origin_wrong_port_rejected():
    assert serve._origin_allowed({"Origin": "http://127.0.0.1:9999"}) is False


def test_origin_null_rejected():
    assert serve._origin_allowed({"Origin": "null"}) is False


def test_origin_trailing_slash_rejected():
    assert serve._origin_allowed({"Origin": f"http://127.0.0.1:{serve.PORT}/"}) is False


def test_origin_uses_bound_port_not_configured(monkeypatch):
    # Regression: when the dynamic port-fallback in main() picks a port
    # other than the configured one (e.g. a sibling project already holds
    # 8765), CSRF must validate against the actually-bound port. Previously
    # _origin_allowed read the static configured PORT, so the second
    # concurrent dashboard rejected every POST with 403.
    monkeypatch.setattr(serve, "PORT", 8765)
    monkeypatch.setattr(serve, "BOUND_PORT", 8766)
    assert serve._origin_allowed({"Origin": "http://localhost:8766"}) is True
    assert serve._origin_allowed({"Origin": "http://127.0.0.1:8766"}) is True
    assert serve._origin_allowed({"Origin": "http://[::1]:8766"}) is True
    assert serve._origin_allowed({"Origin": "http://localhost:8765"}) is False


def test_ws_accept_rejects_bad_origin():
    handler = FakeHandler(
        {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Origin": "http://evil.example.com",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        }
    )

    result = serve.WebSocket.accept(handler)

    assert result is None
    assert handler.errors == [(403, "Origin not allowed")]


def test_pty_create_rejects_bad_origin():
    before = len(serve.PTYS)
    handler = FakeHandler({"Origin": "http://evil.example.com"})
    handler.path = "/api/ptys"
    handler._csrf_guard = lambda: serve.Handler._csrf_guard(handler)
    handler._read_json_body = lambda: pytest.fail("body should not be read")

    serve.Handler.do_POST(handler)

    assert handler.responses == [(403, {"error": "origin not allowed"})]
    assert len(serve.PTYS) == before
