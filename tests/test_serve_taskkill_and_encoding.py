"""Behavioural tests for batch 5: taskkill non-zero rc logging and the
``errors='replace'`` hardening on user-visible file reads.

These exercise the module via ``importlib`` (per conftest sys.path setup)
and monkeypatch ``subprocess.run`` / ``pathlib.Path.read_text`` so we
verify the real code paths, not just the source text.
"""
from __future__ import annotations

import importlib.util
import inspect
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"


def _load_serve():
    spec = importlib.util.spec_from_file_location("serve_batch5_behavioural", SERVE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def serve():
    return _load_serve()


# -------- taskkill failure logging --------

def test_cancel_job_logs_taskkill_nonzero_on_windows(serve, capsys, monkeypatch):
    """Inject a fake JOB entry then call ``_cancel_job``. On Windows the
    cancel path shells out to ``taskkill``. We monkeypatch ``subprocess.run``
    to simulate a non-zero return code and assert the stderr tail is
    printed with the ``[serve] taskkill rc=`` prefix."""
    # Don't touch the persistence file — point it at a tmp.
    tmp_persist = REPO_ROOT / "tests" / "_tmp_jobs_persist.jsonl"
    monkeypatch.setattr(serve, "JOBS_PERSIST_FILE", tmp_persist)

    # Seed a fake running job.
    with serve.JOBS_LOCK:
        serve.JOBS["fake-job-batch5"] = {
            "id": "fake-job-batch5",
            "status": "running",
            "pid": 99999,
            "kind": "test",
            "log_path": str(tmp_persist),
        }

    fake_completed = subprocess.CompletedProcess(
        args=["taskkill", "/F", "/T", "/PID", "99999"],
        returncode=128,
        stdout="",
        stderr="ERROR: The process \"99999\" not found.",
    )

    with mock.patch("subprocess.run", return_value=fake_completed) as m, \
         mock.patch.object(serve.os, "name", "nt"):
        result = serve._cancel_job("fake-job-batch5")

    # Cancel still returns True — best-effort kill.
    assert result is True
    # subprocess.run was called with a list and capture_output=True.
    args, kwargs = m.call_args
    assert args[0][:1] == ["taskkill"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("timeout") == 10
    # The stderr tail was logged.
    captured = capsys.readouterr()
    combined = (captured.out + captured.err)
    assert "[serve] taskkill rc=128" in combined
    assert "99999" in combined

    # Cleanup.
    with serve.JOBS_LOCK:
        serve.JOBS.pop("fake-job-batch5", None)
    if tmp_persist.exists():
        tmp_persist.unlink()


def test_cancel_job_logs_taskkill_timeout(serve, capsys, monkeypatch):
    """If taskkill itself hangs (rare but possible on Windows kernel-stuck
    processes), the timeout=10 catches it and we log the timeout."""
    tmp_persist = REPO_ROOT / "tests" / "_tmp_jobs_persist_timeout.jsonl"
    monkeypatch.setattr(serve, "JOBS_PERSIST_FILE", tmp_persist)

    with serve.JOBS_LOCK:
        serve.JOBS["fake-job-batch5-timeout"] = {
            "id": "fake-job-batch5-timeout",
            "status": "running",
            "pid": 88888,
            "kind": "test",
            "log_path": str(tmp_persist),
        }

    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "taskkill", timeout=10)

    with mock.patch("subprocess.run", side_effect=_raise_timeout), \
         mock.patch.object(serve.os, "name", "nt"):
        result = serve._cancel_job("fake-job-batch5-timeout")

    assert result is True
    captured = capsys.readouterr()
    combined = (captured.out + captured.err)
    assert "[serve] taskkill timed out" in combined
    assert "88888" in combined

    with serve.JOBS_LOCK:
        serve.JOBS.pop("fake-job-batch5-timeout", None)
    if tmp_persist.exists():
        tmp_persist.unlink()


# -------- _load_jsonl_cached tolerates non-UTF-8 bytes --------

def test_load_jsonl_cached_handles_non_utf8(serve, tmp_path):
    """The cache reader (used by every JSONL endpoint, including
    JOBS_PERSIST_FILE) must tolerate stray non-UTF-8 bytes in a line —
    a corrupt entry should not poison the whole endpoint."""
    p = tmp_path / "ledger.jsonl"
    # Mix valid JSON, a non-UTF-8 byte, and another valid JSON line.
    payload = b'{"a": 1}\n\xff\xfe garbage\n{"b": 2}\n'
    p.write_bytes(payload)

    rows = serve._load_jsonl_cached(p)
    # The middle garbage line is dropped by the JSONDecodeError handler;
    # the two valid records survive.
    assert {"a": 1} in rows
    assert {"b": 2} in rows


def test_load_jsonl_cached_empty_when_missing(serve, tmp_path):
    """Missing path returns [] without raising — endpoints rely on this."""
    rows = serve._load_jsonl_cached(tmp_path / "no_such_file.jsonl")
    assert rows == []


# -------- MAX_SSE_SESSION_S is wired through both SSE handlers --------

def test_max_sse_session_s_used_in_job_and_transcript_streams(serve):
    # Use inspect.getsource off the Handler so the check follows handlers that
    # were split out of serve.py into server/handlers/*.py mixins.
    # _handle_job_stream uses it (batch 4 already shipped this).
    job_stream = inspect.getsource(serve.Handler._handle_job_stream)
    assert "MAX_SSE_SESSION_S" in job_stream
    # _handle_transcript_stream now also uses it (batch 5 fix).
    tr_stream = inspect.getsource(serve.Handler._handle_transcript_stream)
    assert "MAX_SSE_SESSION_S" in tr_stream


# -------- response header X-Content-Type-Options: nosniff --------

def test_json_response_emits_nosniff_header(serve):
    """Drive a real ``_json`` call through a fake handler and assert the
    nosniff header is on the wire."""
    import http.server
    import io

    sent_headers: list[tuple[str, str]] = []

    class _Fake:
        wfile = io.BytesIO()

        def send_response(self, status):
            self._status = status

        def send_header(self, k, v):
            sent_headers.append((k, v))

        def end_headers(self):
            pass

    # Bind the unbound method to our fake instance.
    serve.Handler._json(_Fake(), 200, {"ok": True})
    pairs = {k.lower(): v for k, v in sent_headers}
    assert pairs.get("x-content-type-options") == "nosniff"
    assert pairs.get("cache-control") == "no-store"
    assert pairs.get("content-type", "").startswith("application/json")
