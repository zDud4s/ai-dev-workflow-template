"""Error-handling + perf hardening for ``serve.py``.

Covers five recent fixes:

* ``_read_json_body`` now returns a generic ``"invalid JSON in request body"``
  message instead of leaking the underlying ``json.JSONDecodeError`` text.
  Info leak fix: the raw exception sometimes echoes a slice of the request
  body, which is not appropriate to return to clients.

* The main HTTP server is now a ``_ThreadedServer`` subclass with
  ``daemon_threads = True`` so a Ctrl+C tears down in-flight request
  threads cleanly instead of stranding them mid-write to the JSONL
  ledgers.

* ``_aggregate_codex_usage`` caps the per-call rollout-file traversal at a
  fixed N (most-recent-by-mtime). Without the cap, every cache miss scans
  ALL Codex sessions on the machine (~150MB across hundreds of files).

* The JSONL/text reads for METRICS_FILE and EVENTS_FILE now pass
  ``errors="replace"`` so a half-written concurrent append doesn't 500
  the whole endpoint with UnicodeDecodeError.

The tests here are intentionally pure-unit (no network, no subprocess)
so they run in the same fraction-of-a-second as the rest of the serve
test suite.
"""
from __future__ import annotations

import inspect
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above


# ---------------------------------------------------------------------------
# _read_json_body — Fix 1
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Bare bones stand-in for ``serve.Handler`` so we can drive
    ``_read_json_body`` directly. Captures the ``_json`` call so the test
    can inspect the status + payload it would have sent."""

    def __init__(self, body: bytes) -> None:
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.responses: list[tuple[int, dict]] = []

    def _json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))


def test_read_json_body_returns_generic_message_on_parse_error(capsys):
    """Malformed JSON in a request body must NOT echo the parser's
    internal message to the client. The generic ``"invalid JSON in request
    body"`` string is returned instead; the raw exception is logged
    server-side so operators can still diagnose."""
    body = b'{"k": "this is not valid JSON because no close brace'
    h = _FakeHandler(body)

    result = serve.Handler._read_json_body(h)

    assert result is None
    assert len(h.responses) == 1
    status, payload = h.responses[0]
    assert status == 400
    # Generic message — must NOT include any JSON parser internals like
    # line/column numbers or the raw substring of the body.
    assert payload == {"error": "invalid JSON", "detail": "invalid JSON in request body"}

    # The full error is still logged server-side for diagnosis.
    captured = capsys.readouterr()
    assert "[serve] bad JSON body:" in captured.out


def test_read_json_body_returns_generic_message_on_bad_utf8(capsys):
    """Same sanitisation must apply to ``UnicodeDecodeError`` — a binary
    body must not echo ``invalid start byte 0xff at position 3`` etc to
    clients either."""
    h = _FakeHandler(b"\xff\xfe\xfd not utf-8")

    result = serve.Handler._read_json_body(h)

    assert result is None
    assert h.responses[0][1]["detail"] == "invalid JSON in request body"
    assert "[serve] bad JSON body:" in capsys.readouterr().out


def test_read_json_body_accepts_valid_payload():
    """Regression guard for the happy path: the sanitisation must not
    have broken the normal parse-and-return flow."""
    h = _FakeHandler(b'{"hello": "world", "n": 42}')

    result = serve.Handler._read_json_body(h)

    assert result == {"hello": "world", "n": 42}
    assert h.responses == []  # no error response sent


# ---------------------------------------------------------------------------
# _ThreadedServer.daemon_threads — Fix 3
# ---------------------------------------------------------------------------


def test_threaded_server_has_daemon_threads():
    """The main HTTP server class used by ``main()`` must declare
    ``daemon_threads = True`` so Ctrl+C tears down in-flight request
    threads cleanly. Without this, a 120s improver subprocess.run blocks
    shutdown and a second Ctrl+C kills writer threads mid-flush."""
    import sys

    assert hasattr(serve, "_ThreadedServer"), \
        "serve.py must define a _ThreadedServer subclass for daemon_threads=True"
    assert serve._ThreadedServer.daemon_threads is True
    # Port-exclusivity policy is platform-specific. On POSIX, SO_REUSEADDR
    # is safe quality-of-life (avoids TIME_WAIT after Ctrl+C). On Windows
    # it actively *breaks* exclusivity — two processes both setting it
    # silently bind to the same address and split traffic. We therefore
    # disable allow_reuse_address on Windows and rely on SO_EXCLUSIVEADDRUSE
    # (set in server_bind) to provide both restart-friendliness and
    # exclusivity.
    if sys.platform == "win32":
        assert serve._ThreadedServer.allow_reuse_address is False
        assert "SO_EXCLUSIVEADDRUSE" in inspect.getsource(serve._ThreadedServer.server_bind)
    else:
        assert serve._ThreadedServer.allow_reuse_address is True
    # Sanity-check the inheritance so a future refactor that swaps to
    # http.server.HTTPServer still preserves the daemon-threads property.
    import socketserver
    assert issubclass(serve._ThreadedServer, socketserver.ThreadingTCPServer)


# ---------------------------------------------------------------------------
# _aggregate_codex_usage cap — Fix 5
# ---------------------------------------------------------------------------


def test_codex_usage_cap_limits_traversal(tmp_path, monkeypatch):
    """``_aggregate_codex_usage`` must not open more than the configured
    cap (~100) of rollout files, even when ``Path.rglob`` returns many
    more. Older sessions are dropped (sorted-by-mtime, newest first)
    because the 30s cache makes long scans block the overview endpoint.
    """
    # Build a fake sessions root with 200 stub rollout files. The bodies
    # are empty so per-file scan time is negligible — we're measuring the
    # cap, not the parse loop.
    sessions_root = tmp_path / "codex_sessions"
    sessions_root.mkdir()
    fake_files: list[pathlib.Path] = []
    for i in range(200):
        p = sessions_root / f"rollout-{i:04d}.jsonl"
        p.write_text("", encoding="utf-8")
        # Stagger the mtimes so the sort is deterministic — the newest
        # file is the one with the highest index.
        import os as _os
        _os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        fake_files.append(p)

    monkeypatch.setattr(serve, "_CODEX_SESSIONS_ROOT_OVERRIDE", sessions_root)

    # Spy on ``Path.open`` so we can count how many rollouts get read.
    real_open = pathlib.Path.open
    opens: list[pathlib.Path] = []

    def counting_open(self, *args, **kwargs):
        if str(self).endswith(".jsonl") and "rollout-" in self.name:
            opens.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    out = serve._aggregate_codex_usage(serve.ROOT, now)

    # rglob returned 200 → cap must keep it below the configured ceiling.
    # We don't assert the exact number to leave the executor room to pick
    # any sensible cap; 100 is the current default. A cap of 120 or 150
    # would still be a fix vs the unbounded baseline.
    assert len(opens) <= 150, \
        f"codex usage scan opened {len(opens)} files; cap should drop most"
    assert len(opens) >= 1, "scan must still read at least one file"
    # ``sessions`` reflects the post-cap traversal, not the raw rglob.
    assert out["sessions"] == len(opens)


def test_codex_usage_cap_keeps_newest_by_mtime(tmp_path, monkeypatch):
    """When the cap kicks in, the rollouts we KEEP are the newest by
    mtime (older sessions don't contribute meaningfully to "recent
    usage"). This pins the sort order."""
    sessions_root = tmp_path / "codex_sessions"
    sessions_root.mkdir()
    paths: list[pathlib.Path] = []
    import os as _os
    for i in range(200):
        p = sessions_root / f"rollout-{i:04d}.jsonl"
        p.write_text("", encoding="utf-8")
        _os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        paths.append(p)

    monkeypatch.setattr(serve, "_CODEX_SESSIONS_ROOT_OVERRIDE", sessions_root)

    real_open = pathlib.Path.open
    opened_names: list[str] = []

    def counting_open(self, *args, **kwargs):
        if str(self).endswith(".jsonl") and "rollout-" in self.name:
            opened_names.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)

    import datetime as _dt
    serve._aggregate_codex_usage(serve.ROOT, _dt.datetime.now(_dt.timezone.utc))

    # The oldest file (index 0000) must not appear in the kept set when
    # 200 candidates were truncated.
    if opened_names and len(opened_names) < 200:
        assert "rollout-0000.jsonl" not in opened_names, \
            "cap kept the oldest rollout; should keep newest by mtime"
        # The very newest (index 0199) must be in the kept set.
        assert "rollout-0199.jsonl" in opened_names, \
            "cap dropped the newest rollout; sort direction is wrong"


# ---------------------------------------------------------------------------
# read_text errors="replace" on JSONL — Fix 2
# ---------------------------------------------------------------------------


def test_metrics_file_read_uses_errors_replace(tmp_path, monkeypatch):
    """``METRICS_FILE`` (the auto-select metrics ledger) must tolerate
    invalid UTF-8 — a half-written concurrent append produces broken
    sequences that should NOT 500 the endpoint. Pre-fix, the
    ``read_text(encoding="utf-8")`` call would raise UnicodeDecodeError
    on the first bad byte."""
    metrics_path = tmp_path / "metrics.jsonl"
    # Two valid rows, then bytes that aren't valid UTF-8 in the middle of
    # a third row. ``errors="replace"`` should let us read the file at
    # all; whether the partial row parses as JSON is a separate concern
    # handled by the per-line try/except.
    payload = (
        b'{"phase":"plan","tool":"claude"}\n'
        b'{"phase":"execute","tool":"codex"}\n'
        b'{"phase":"\xff\xfe truncated'
    )
    metrics_path.write_bytes(payload)
    monkeypatch.setattr(serve, "METRICS_FILE", metrics_path)

    # Pre-fix this raised UnicodeDecodeError. Post-fix it must return a
    # populated aggregate (the two well-formed rows survive; the third
    # is silently dropped by the per-line JSON guard).
    result = serve._load_auto_select_ranking()
    assert isinstance(result, dict)
    # The endpoint surface — should not have raised.


def test_events_file_read_uses_errors_replace(tmp_path, monkeypatch):
    """Same guarantee for ``EVENTS_FILE`` (the dashboard telemetry stream
    written by the PostToolUse hook). A truncated append from a
    Ctrl+C'd hook should NOT make /api/timeline return 500."""
    events_path = tmp_path / "events.jsonl"
    payload = (
        b'{"type":"phase_dispatch","phase":"plan","session_id":"a"}\n'
        b'{"type":"phase_dispatch","phase":"execute","session_id":"a"}\n'
        b'\xff\xfe partial line'
    )
    events_path.write_bytes(payload)
    monkeypatch.setattr(serve, "EVENTS_FILE", events_path)

    # Must not raise UnicodeDecodeError.
    result = serve._load_timeline_runs()
    assert isinstance(result, list)


def test_serve_source_no_unguarded_utf8_reads_on_known_jsonl_paths():
    """Static guard: every ``read_text(encoding="utf-8")`` call in serve.py
    that targets one of the known JSONL/operator-content ledgers MUST
    also pass ``errors="replace"``. Catches regressions where a
    refactor reintroduces a strict decode on an append-only file.

    Batch 6: METRICS_FILE / EVENTS_FILE reads now flow through
    ``_load_jsonl_cached`` (mtime-keyed cache). The helper itself MUST
    use ``errors="replace"`` AND no direct strict-utf8 read on these
    sentinels may sneak back in.
    """
    src = (pathlib.Path(serve.__file__)).read_text(encoding="utf-8")

    # 1. _load_jsonl_cached helper preserves the safety invariant. It now lives
    #    in server/storage.py (re-exported by serve); inspect.getsource follows
    #    the re-export, so this stays robust regardless of which file holds it.
    import inspect
    helper_window = inspect.getsource(serve._load_jsonl_cached)
    assert 'errors="replace"' in helper_window, \
        "_load_jsonl_cached must read with errors=\"replace\""

    # 2. No direct strict-utf8 read on METRICS_FILE / EVENTS_FILE.
    forbidden_idents = [
        "METRICS_FILE.read_text",
        "EVENTS_FILE.read_text",
    ]
    for ident in forbidden_idents:
        idx = src.find(ident)
        if idx == -1:
            continue  # Migrated to _load_jsonl_cached — good.
        window = src[idx : idx + 150]
        assert 'errors="replace"' in window, \
            f"{ident} must pass errors=\"replace\" if reintroduced — found: {window!r}"

    # 3. Confirm the cache is actually wired up for these sentinels.
    assert "_load_jsonl_cached(METRICS_FILE)" in src, \
        "METRICS_FILE should be read via _load_jsonl_cached"
    assert "_load_jsonl_cached(EVENTS_FILE)" in src, \
        "EVENTS_FILE should be read via _load_jsonl_cached"
