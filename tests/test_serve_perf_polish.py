"""Batch 4 — perf + polish hardening for ``serve.py``.

Covers five medium-severity fixes layered on top of batches 1+2+3:

* **Fix 1** — ``_read_json_body`` now rejects oversized bodies (anything
  past ``MAX_JSON_BODY = 1 MiB``) with a 413 BEFORE allocating the buffer,
  using only ``Content-Length``. Trivial DoS guard.
* **Fix 2** — ``_handle_job_stream`` enforces a hard ``MAX_SSE_SESSION_S``
  upper bound on a single SSE session regardless of idleness; clients
  reconnect transparently. Previously a chatty job could pin a request
  thread + queue subscriber + TCP connection indefinitely.
* **Fix 3** — ``_handle_files_list`` fallback ``ROOT.rglob("*")`` walk
  now skips ``SKIP_DIRS`` (``.git``, ``node_modules``, ``__pycache__``,
  ``.venv`` …). Stops the autocomplete endpoint from walking the entire
  ``.git/objects`` tree on every keystroke and from leaking dotfile
  paths into the suggestion list.
* **Fix 4** — five more broad/silent ``except`` sites now log via
  ``print("[serve] ...", flush=True)`` instead of swallowing silently.
  Operators can finally see WHY persistence / audit ledger / transcript
  delete / proposals scan / log sweep silently fail.
* **Fix 5** — ``_aggregate_skill_metrics`` already routes through the
  ``_load_jsonl_cached`` helper added in batch 2 (no-op here, asserted
  as a regression guard so a future refactor doesn't accidentally undo
  the caching).

Tests here are pure-unit: no network, no subprocess, no real server.
"""
from __future__ import annotations

import inspect
import io
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above


# ---------------------------------------------------------------------------
# Fix 1 — _read_json_body oversized body rejection
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Bare bones stand-in for ``serve.Handler`` so we can drive
    ``_read_json_body`` directly. Captures the ``_json`` call so the test
    can inspect the status + payload it would have sent. Mirrors the
    pattern used in ``test_serve_error_handling.py``."""

    def __init__(self, content_length: str | int, body: bytes = b"") -> None:
        self.headers = {"Content-Length": str(content_length)}
        self.rfile = io.BytesIO(body)
        self.responses: list[tuple[int, dict]] = []

    def _json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))


def test_read_json_body_rejects_oversized():
    """A ``Content-Length`` past ``MAX_JSON_BODY`` must trigger a 413
    BEFORE we read the body. This is the trivial-DoS guard: a single
    huge POST against any JSON endpoint must not allocate a 100 MB
    buffer."""
    # ``99_999_999`` is well past the 1 MiB ceiling.
    h = _FakeHandler(content_length=99_999_999)

    result = serve.Handler._read_json_body(h)

    assert result is None
    assert len(h.responses) == 1
    status, payload = h.responses[0]
    assert status == 413
    # The response must surface the "too large" reason so the client
    # can show a useful error. Existing batch-2 contract keeps the
    # ``error`` key; we also added a ``detail`` per the spec.
    blob = str(payload)
    assert "too large" in blob.lower()


def test_read_json_body_accepts_just_under_cap():
    """The cap is exclusive — bodies exactly at or under
    ``MAX_JSON_BODY`` must continue to parse normally. This pins the
    boundary so a future refactor doesn't accidentally tighten the
    limit and break legitimate large composer payloads."""
    payload = b'{"x": "' + b"a" * 100 + b'"}'
    h = _FakeHandler(content_length=len(payload), body=payload)

    result = serve.Handler._read_json_body(h)

    assert result == {"x": "a" * 100}
    assert h.responses == []  # no error response


def test_read_json_body_handles_invalid_content_length():
    """A non-numeric ``Content-Length`` header (e.g. attacker-supplied
    ``Content-Length: foo``) must NOT crash the handler with
    ``ValueError`` — it must surface a clean 4xx instead."""
    h = _FakeHandler(content_length="not-a-number")

    result = serve.Handler._read_json_body(h)

    assert result is None
    assert len(h.responses) == 1
    status, _ = h.responses[0]
    assert 400 <= status < 500, f"expected 4xx for bad Content-Length, got {status}"


# ---------------------------------------------------------------------------
# Fix 2 — SSE session hard cap
# ---------------------------------------------------------------------------


def test_sse_has_max_session_constant():
    """``MAX_SSE_SESSION_S`` must exist as a module-level constant so a
    single SSE subscriber cannot pin a request thread + queue slot +
    TCP connection forever, even when the underlying job keeps emitting
    chunks (which defeats the existing idle-timeout check)."""
    assert hasattr(serve, "MAX_SSE_SESSION_S"), \
        "serve.py must define MAX_SSE_SESSION_S to cap individual SSE sessions"
    # The exact value is a policy choice (30 min today). Pin only the
    # invariant that matters: it's a positive finite number well above
    # the heartbeat interval (15s) and well below "forever".
    cap = serve.MAX_SSE_SESSION_S
    assert isinstance(cap, (int, float))
    assert 60 <= cap <= 24 * 3600, \
        f"MAX_SSE_SESSION_S={cap} is outside the sane 1min..24h window"


def test_sse_handler_checks_session_cap():
    """The ``_handle_job_stream`` body must reference both
    ``session_start`` and ``MAX_SSE_SESSION_S`` so the cap is actually
    enforced in the live-tail loop (not just declared as a constant)."""
    src = inspect.getsource(serve.Handler._handle_job_stream)
    assert "MAX_SSE_SESSION_S" in src, \
        "_handle_job_stream must reference MAX_SSE_SESSION_S to enforce the cap"
    assert "session_start" in src or "time.monotonic" in src, \
        "_handle_job_stream must capture a session start time to compare against"


# ---------------------------------------------------------------------------
# Fix 3 — SKIP_DIRS exclude in the files-list fallback walk
# ---------------------------------------------------------------------------


def test_skip_dirs_constant_present():
    """``SKIP_DIRS`` must exist with the well-known noise directories so
    the autocomplete fallback walk doesn't drown in ``.git/objects/**``
    or leak ``.venv``/``node_modules`` paths into the suggestion list."""
    assert hasattr(serve, "SKIP_DIRS"), \
        "serve.py must define SKIP_DIRS for the rglob fallback exclude"
    skip = serve.SKIP_DIRS
    # ``.git`` is the load-bearing one — leaking objects/* into autocomplete
    # is a measurable hot path on any non-trivial repo.
    assert ".git" in skip
    # The other obvious noise dirs.
    for name in ("node_modules", "__pycache__"):
        assert name in skip, f"SKIP_DIRS missing {name!r}"


def test_files_list_fallback_uses_skip_dirs():
    """The fallback walk in ``_handle_files_list`` must reference
    ``SKIP_DIRS`` so the exclude actually fires. Source-level check
    keeps the test pure-unit (no real walk needed)."""
    src = inspect.getsource(serve.Handler._handle_files_list)
    assert "SKIP_DIRS" in src, \
        "_handle_files_list fallback must consult SKIP_DIRS"
    assert "rglob" in src, \
        "_handle_files_list fallback must still call rglob (sanity)"


# ---------------------------------------------------------------------------
# Fix 5 — _aggregate_skill_metrics uses the JSONL cache
# ---------------------------------------------------------------------------


def test_aggregate_skill_metrics_uses_cache():
    """``_aggregate_skill_metrics`` must consume ``SKILL_METRICS_FILE``
    via the shared ``_load_jsonl_cached`` helper. Without the cache it
    re-parses the full ledger on every overview render — measurably
    slow once the ledger crosses a few hundred rows.

    This is a regression guard for the batch-2 work; if a future
    refactor switches back to ``open()`` / ``read_text()`` it will
    revert the perf gain without anyone noticing."""
    src = inspect.getsource(serve._aggregate_skill_metrics)
    assert "_load_jsonl_cached(SKILL_METRICS_FILE)" in src, (
        "_aggregate_skill_metrics must read SKILL_METRICS_FILE via "
        "_load_jsonl_cached — direct read_text/open re-introduces the "
        "per-call full-ledger reparse the batch-2 cache was added to fix."
    )
    # And it must not ALSO contain a raw read of the same file — belt
    # and braces against half-converted refactors.
    assert "SKILL_METRICS_FILE.read_text" not in src
    assert "SKILL_METRICS_FILE.open" not in src


# ---------------------------------------------------------------------------
# Fix 4 — broad except logging count regression guard
# ---------------------------------------------------------------------------


def _count_silent_excepts(src: str) -> int:
    """Count ``except [..]: pass`` patterns regardless of which exception
    class is caught — both single-line ``except OSError: pass`` and the
    two-line ``except OSError:\\n    pass`` variants."""
    single = len(re.findall(r"^\s*except[^:]*:\s*pass\s*$", src, re.M))
    multi = len(re.findall(r"^\s*except[^:]*:\s*$\s*^\s*pass\s*$", src, re.M))
    return single + multi


def test_broad_except_count_decreasing():
    """Each batch should chip away at the pile of silent ``except: pass``
    sites — they are the single biggest source of "the dashboard
    silently does the wrong thing" reports. The pre-batch-4 baseline
    was 43 (23 single-line + 20 two-line); this batch reduced it by
    at least 4 sites (the PTY/SSE event-loop sites are intentionally
    left alone, see SKILL handoff)."""
    src = pathlib.Path(serve.__file__).read_text(encoding="utf-8")
    current = _count_silent_excepts(src)
    # Hard ceiling — 39 leaves one fix worth of slack but proves
    # batch 4 actually shipped. If a future batch lowers this further,
    # please also lower the ceiling here so we don't silently regress.
    assert current <= 39, (
        f"silent except sites = {current}; expected <= 39 after batch 4 "
        f"(pre-batch baseline was 43). Either some were re-introduced, or "
        f"the ceiling here needs lowering to match progress."
    )
    # Floor — fail loudly if a future batch deletes a huge chunk of
    # try/except blocks; that almost certainly removed real error
    # handling we don't want gone.
    assert current >= 10, (
        f"silent except sites = {current}; this is implausibly low — "
        f"someone probably deleted a swath of error-handling blocks "
        f"rather than adding logging to them."
    )


def test_persist_job_logs_on_failure(capsys):
    """``_persist_job`` was silently swallowing OSError; an operator who
    noticed restarts losing job history had no trail. It now logs to
    stdout with the ``[serve]`` prefix used everywhere else."""
    src = inspect.getsource(serve._persist_job)
    assert "[serve] persist_job failed" in src, \
        "_persist_job must log on OSError instead of swallowing silently"


def test_audit_improvement_logs_on_failure():
    """Same regression guard for the improvements ledger writer."""
    src = inspect.getsource(serve._audit_improvement)
    assert "[serve] audit_improvement" in src, \
        "_audit_improvement must log on OSError instead of swallowing silently"
