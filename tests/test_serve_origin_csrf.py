import inspect

import pytest

import serve


POST_HANDLER_NAMES = (
    "_handle_memory",
    "_handle_decisions",
    "_handle_events_clear",
    "_handle_dispatch_mode",
    "_handle_phase_update",
    "_handle_jobs_create",
    "_handle_workflow_check",
    "_handle_workflow_update",
    "_handle_improver_update",
    "_handle_auto_select_update",
    "_handle_pty_create",
    "_handle_job_cancel",
    "_handle_job_input",
    "_handle_pty_kill",
    "_handle_job_interrupt",
    "_handle_proposal_decision",
    "_handle_suggestion_draft",
    "_handle_agent_suggest",
    "_handle_agent_proposal_decision",
)


class FakeHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.errors = []
        self.responses = []

    def send_error(self, code, msg=None):
        self.errors.append((code, msg))

    def _json(self, code, payload):
        self.responses.append((code, payload))


def _handler(path: str, headers: dict[str, str]):
    handler = serve.Handler.__new__(serve.Handler)
    handler.path = path
    handler.headers = headers
    handler.responses = []
    handler.body_read = False
    handler.called = []
    return handler


def _record_json(self, code, payload):
    self.responses.append((code, payload))


def _patch_post_handlers_to_fail(monkeypatch):
    def fail(self, *args, **kwargs):
        pytest.fail(f"unexpected POST route handler reached: {args!r} {kwargs!r}")

    for name in POST_HANDLER_NAMES:
        monkeypatch.setattr(serve.Handler, name, fail)


def _assert_post_rejected_before_body(monkeypatch, path: str, headers: dict[str, str]):
    handler = _handler(path, headers)
    monkeypatch.setattr(serve.Handler, "_json", _record_json)

    def read_body(self):
        self.body_read = True
        pytest.fail("body should not be read when Origin is rejected")

    monkeypatch.setattr(serve.Handler, "_read_json_body", read_body)
    _patch_post_handlers_to_fail(monkeypatch)

    serve.Handler.do_POST(handler)

    assert handler.responses == [(403, {"error": "origin not allowed"})]
    assert handler.body_read is False
    assert handler.called == []


def _assert_post_allowed(monkeypatch, path: str, origin: str, target_handler: str):
    handler = _handler(path, {"Origin": origin})
    monkeypatch.setattr(serve.Handler, "_json", _record_json)

    def read_body(self):
        self.body_read = True
        return {}

    def record_call(self, *args):
        self.called.append((target_handler, args))

    monkeypatch.setattr(serve.Handler, "_read_json_body", read_body)
    _patch_post_handlers_to_fail(monkeypatch)
    monkeypatch.setattr(serve.Handler, target_handler, record_call)

    serve.Handler.do_POST(handler)

    assert handler.responses == []
    assert handler.body_read is True
    assert handler.called
    assert handler.called[0][0] == target_handler


@pytest.mark.parametrize(
    "path",
    (
        "/api/jobs",
        "/api/memory",
        "/api/ptys",
        "/api/jobs/123e4567-e89b-12d3-a456-426614174000/cancel",
    ),
)
def test_post_absent_origin_rejected_uniformly(monkeypatch, path):
    _assert_post_rejected_before_body(monkeypatch, path, {})


def test_post_cross_site_origin_rejected(monkeypatch):
    _assert_post_rejected_before_body(
        monkeypatch,
        "/api/jobs",
        {"Origin": "http://evil.example.com"},
    )


def test_post_ipv6_loopback_allowed(monkeypatch):
    _assert_post_allowed(
        monkeypatch,
        "/api/ptys",
        f"http://[::1]:{serve.PORT}",
        "_handle_pty_create",
    )


@pytest.mark.parametrize(
    "origin",
    (
        f"http://localhost:{serve.PORT}",
        f"http://127.0.0.1:{serve.PORT}",
    ),
)
def test_post_localhost_and_ipv4_allowed(monkeypatch, origin):
    _assert_post_allowed(monkeypatch, "/api/jobs", origin, "_handle_jobs_create")


def test_ws_upgrade_absent_origin_rejected():
    handler = FakeHandler(
        {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        }
    )

    result = serve.WebSocket.accept(handler)

    assert result is None
    assert handler.errors == [(403, "Origin not allowed")]


def test_pty_create_no_redundant_origin_check():
    source = inspect.getsource(serve.Handler._handle_pty_create)

    assert "_origin_allowed" not in source


def test_get_unaffected_by_missing_origin(monkeypatch):
    handler = _handler("/api/jobs", {})

    def record_jobs_list(self):
        self.called.append("_handle_jobs_list")

    monkeypatch.setattr(serve.Handler, "_handle_jobs_list", record_jobs_list)

    serve.Handler.do_GET(handler)

    assert handler.called == ["_handle_jobs_list"]
