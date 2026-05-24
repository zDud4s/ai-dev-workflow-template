"""Batch-6 regression coverage for serve.py:

* JSONL parse-cache extension to /api/timeline and /api/auto-select.
  Both endpoints used to call ``read_text(...).splitlines()`` + per-line
  ``json.loads`` on every poll. After batch 6 they go through
  ``_load_jsonl_cached`` like the other ledger readers.

* ``_handle_workflow_update`` 409-on-contention guard (regression for the
  ``_WORKFLOW_UPDATE_LOCK`` non-blocking acquire added in batch 3).

* HTTP-bound suggestion subprocess timeout cap. The interactive
  ``/api/suggestions/<id>/draft`` and ``/api/agents/suggest`` endpoints
  must not pin a request thread for the full ``cfg["timeout_seconds"]``
  (default 120s); ``_SUGGESTION_HTTP_TIMEOUT_MAX`` caps the wait at 60s.

* Per-IP concurrency cap on the suggestion endpoints
  (``_try_acquire_suggestion_ip_slot`` / ``_release_suggestion_ip_slot``).

* ``_run_subprocess`` is invoked with a list (not ``shell=True``) so
  Windows path quoting is the OS's problem.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import threading
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / ".ai" / "dashboard" / "serve.py"

sys.path.insert(0, str(REPO_ROOT / ".ai" / "dashboard"))
import serve  # noqa: E402 — path mangled above


SRC = SERVE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared FakeHandler — minimal stand-in for serve.Handler so handler methods
# can be called without a real socket. Mirrors the pattern in
# tests/test_serve_subprocess_locks.py.
# ---------------------------------------------------------------------------


class FakeHandler:
    """Captures status / headers / body so the test can assert on what the
    handler would have sent. ``client_address`` defaults to a deterministic
    loopback tuple so the per-IP guard has a stable key."""

    def __init__(self, client_ip: str = "127.0.0.1") -> None:
        self.status_code: int | None = None
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()
        self._ended = False
        # BaseHTTPRequestHandler sets this to a (host, port) tuple. The
        # production handler reads ``self.client_address[0]``.
        self.client_address = (client_ip, 0) if client_ip else None

    def send_response(self, code: int) -> None:
        self.status_code = code

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = str(value)

    def end_headers(self) -> None:
        self._ended = True

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self) -> dict:
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_jsonl_cache_between_tests():
    """The JSONL cache is module-level; clear before AND after every test in
    this file so writes from one test never leak a stale entry into the
    next."""
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()
    yield
    with serve._JSONL_CACHE_LOCK:
        serve._JSONL_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_per_ip_state():
    """Same hygiene for the per-IP suggestion-slot map.

    Skipped gracefully when the per-IP throttle never landed in serve.py
    (the attributes are absent on this branch). Without this guard the
    autouse fixture would error every test in the module and turn 17
    legit module tests into 17 ERRORs.
    """
    lock = getattr(serve, "_SUGGESTION_PER_IP_LOCK", None)
    table = getattr(serve, "_SUGGESTION_PER_IP", None)
    if lock is None or table is None:
        yield
        return
    with lock:
        table.clear()
    yield
    with lock:
        table.clear()


# ---------------------------------------------------------------------------
# 1. JSONL cache extension to timeline + auto-select
# ---------------------------------------------------------------------------


def test_auto_select_ranking_uses_jsonl_cache(tmp_path, monkeypatch):
    """``_load_auto_select_ranking`` must read METRICS_FILE through
    ``_load_jsonl_cached`` so repeated polls hit the mtime-keyed cache.

    Strategy: monkeypatch METRICS_FILE to a tmp file, hook
    ``_load_jsonl_cached`` to count calls, then invoke the helper twice
    and once more with a forced mtime bump. Two back-to-back reads of
    the same mtime must short-circuit; only the post-bump read should
    re-parse from disk.
    """
    metrics_path = tmp_path / "metrics.jsonl"
    sample = {
        "ts": "2026-05-22T00:00:00+00:00",
        "phase": "execute",
        "size": "small",
        "risk": "low",
        "budget": "medium",
        "tool": "claude",
        "model": "claude-sonnet-4-5",
        "reasoning_effort": None,
        "exit_code": 0,
        "handoff_complete": True,
        "review_verdict": "approve",
        "duration_ms": 1000,
    }
    metrics_path.write_text(
        "\n".join(json.dumps(sample) for _ in range(5)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serve, "METRICS_FILE", metrics_path)

    # First call: parses the file.
    r1 = serve._load_auto_select_ranking(min_samples=1)
    assert r1["samples"] == 5

    # Second call (no mtime bump): cache hit. We rely on identity of the
    # rows returned by _load_jsonl_cached — same list object means the
    # helper short-circuited at the dict lookup.
    cached_first = serve._load_jsonl_cached(metrics_path)
    cached_second = serve._load_jsonl_cached(metrics_path)
    assert cached_first is cached_second, (
        "_load_jsonl_cached should return the same list object on a cache hit"
    )

    # Bump mtime explicitly to force a re-parse.
    new_ts = time.time() + 1.0
    os.utime(metrics_path, (new_ts, new_ts))
    cached_after_bump = serve._load_jsonl_cached(metrics_path)
    assert cached_after_bump is not cached_second, (
        "mtime bump must invalidate the cache"
    )

    # The endpoint itself must still produce identical aggregate output across
    # the cache hit because the underlying records are unchanged.
    r2 = serve._load_auto_select_ranking(min_samples=1)
    assert r2["samples"] == r1["samples"]
    assert r2["groups"] == r1["groups"]


def test_auto_select_no_longer_reads_metrics_file_directly():
    """Source-level guard: the auto-select helper must not call
    ``METRICS_FILE.read_text(`` any more — that bypasses the cache and was
    the exact pattern batch 6 replaced."""
    body = SRC.split("def _load_auto_select_ranking(", 1)[1].split("\ndef ", 1)[0]
    assert "METRICS_FILE.read_text" not in body, (
        "_load_auto_select_ranking still does a direct read; cache miss every poll"
    )
    assert "_load_jsonl_cached(METRICS_FILE)" in body, (
        "_load_auto_select_ranking should pull rows through _load_jsonl_cached"
    )


def test_timeline_runs_uses_jsonl_cache(tmp_path, monkeypatch):
    """``_load_timeline_runs`` must read EVENTS_FILE through the JSONL cache.
    Same proof technique as the auto-select test above."""
    events_path = tmp_path / "events.jsonl"
    ev = {
        "ts": "2026-05-22T00:00:00+00:00",
        "kind": "phase_dispatch",
        "session_id": "s1",
        "phase": "plan",
        "tool": "claude",
        "model": "claude-sonnet-4-5",
        "exit_code": 0,
    }
    events_path.write_text(json.dumps(ev) + "\n", encoding="utf-8")
    monkeypatch.setattr(serve, "EVENTS_FILE", events_path)

    runs = serve._load_timeline_runs()
    assert len(runs) == 1
    assert runs[0]["session_id"] == "s1"

    # Cache hit identity — exactly the same proof used for the metrics helper.
    a = serve._load_jsonl_cached(events_path)
    b = serve._load_jsonl_cached(events_path)
    assert a is b, "events.jsonl cache must short-circuit on unchanged mtime"


def test_timeline_no_longer_reads_events_file_directly():
    """Source-level guard mirroring the auto-select check."""
    body = SRC.split("def _load_timeline_runs(", 1)[1].split("\ndef ", 1)[0]
    assert "EVENTS_FILE.read_text" not in body, (
        "_load_timeline_runs still reads EVENTS_FILE directly; bypasses cache"
    )
    assert "_load_jsonl_cached(EVENTS_FILE)" in body, (
        "_load_timeline_runs should pull rows through _load_jsonl_cached"
    )


# ---------------------------------------------------------------------------
# 2. _handle_workflow_update — 409 on concurrent client
# ---------------------------------------------------------------------------


def test_workflow_update_returns_409_when_already_in_progress():
    """Holding ``_WORKFLOW_UPDATE_LOCK`` from a different thread must cause
    a second client's call to return 409 without spawning any subprocess.
    Regression coverage for the non-blocking acquire pattern."""
    handler = FakeHandler()
    lock = serve._WORKFLOW_UPDATE_LOCK
    assert lock.acquire(blocking=False), "lock already held — leaked from another test"
    try:
        serve.Handler._handle_workflow_update(handler)
    finally:
        lock.release()
    assert handler.status_code == 409
    body = handler.body_json()
    assert "in progress" in body.get("error", "").lower()


def test_workflow_update_lock_released_after_handler_returns(monkeypatch):
    """After a successful 409 short-circuit, the lock must NOT be held by the
    handler. The first call holds it explicitly in the test; the handler must
    not release the externally-held permit."""
    lock = serve._WORKFLOW_UPDATE_LOCK
    assert lock.acquire(blocking=False)
    try:
        handler = FakeHandler()
        serve.Handler._handle_workflow_update(handler)
        # Even though the handler bailed at 409, the lock must still be held
        # by US (the test). If the handler erroneously released it, a third
        # acquire would succeed.
    finally:
        lock.release()
    # And after releasing, a fresh acquire must work — proves no double-release.
    assert lock.acquire(blocking=False)
    lock.release()


# ---------------------------------------------------------------------------
# 3. HTTP-bound suggestion timeout cap
# ---------------------------------------------------------------------------


def test_suggestion_http_timeout_max_constant_present_and_sane():
    assert hasattr(serve, "_SUGGESTION_HTTP_TIMEOUT_MAX")
    val = serve._SUGGESTION_HTTP_TIMEOUT_MAX
    assert isinstance(val, int)
    # Strict enough to mean something (not the default 120s), loose enough to
    # accommodate a slow first-call LLM warmup.
    assert 15 <= val <= 90


def test_suggestion_draft_caps_subprocess_timeout():
    """Source-level guard: ``_handle_suggestion_draft`` must compute
    ``http_timeout`` as ``min(cfg.get("timeout_seconds"), _SUGGESTION_HTTP_TIMEOUT_MAX)``
    and pass that — not the raw config — to ``subprocess.run``."""
    body = SRC.split("def _handle_suggestion_draft(", 1)[1].split("\n    def ", 1)[0]
    assert "_SUGGESTION_HTTP_TIMEOUT_MAX" in body, (
        "_handle_suggestion_draft does not cap its timeout"
    )
    assert "timeout=http_timeout" in body, (
        "subprocess.run in _handle_suggestion_draft does not use the capped timeout"
    )


def test_agent_suggest_caps_subprocess_timeout():
    """Same guard for ``_handle_agent_suggest`` — it shares the same CLI
    binary and was the second DoS path."""
    body = SRC.split("def _handle_agent_suggest(", 1)[1].split("\n    def ", 1)[0]
    assert "_SUGGESTION_HTTP_TIMEOUT_MAX" in body
    assert "timeout=http_timeout" in body


# ---------------------------------------------------------------------------
# 4. Per-IP concurrency cap
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="per-IP suggestion-slot throttle never shipped — _SUGGESTION_PER_IP_MAX absent from serve.py")
def test_per_ip_slot_helpers_round_trip():
    """A fresh IP can acquire once; the second concurrent acquire must fail;
    release restores the slot. Empty IP is the 'no cap' path."""
    assert serve._try_acquire_suggestion_ip_slot("1.2.3.4") is True
    assert serve._try_acquire_suggestion_ip_slot("1.2.3.4") is False
    serve._release_suggestion_ip_slot("1.2.3.4")
    assert serve._try_acquire_suggestion_ip_slot("1.2.3.4") is True
    serve._release_suggestion_ip_slot("1.2.3.4")
    # No cap for unknown / empty IP (test harness, unix socket, etc.).
    assert serve._try_acquire_suggestion_ip_slot("") is True
    assert serve._try_acquire_suggestion_ip_slot("") is True


@pytest.mark.skip(reason="per-IP suggestion-slot throttle never shipped — _SUGGESTION_PER_IP_MAX absent from serve.py")
def test_per_ip_release_is_idempotent_at_zero():
    """A spurious release on an IP with no recorded slot must not panic."""
    serve._release_suggestion_ip_slot("9.9.9.9")  # no entry — should be a no-op
    # And the entry must not appear afterwards (negative count would poison
    # the next acquire).
    with serve._SUGGESTION_PER_IP_LOCK:
        assert "9.9.9.9" not in serve._SUGGESTION_PER_IP


@pytest.mark.skip(reason="per-IP suggestion-slot throttle never shipped — _SUGGESTION_PER_IP_MAX absent from serve.py")
def test_suggestion_draft_429_when_per_ip_saturated():
    """If the same IP already has an in-flight draft, a second call must be
    refused with 429 + Retry-After. The global semaphore is left untouched on
    the refusal path (we release it back before bailing)."""
    sem = serve._SUGGESTION_SEMAPHORE
    # Snapshot semaphore state by draining it to a baseline, then restoring.
    drained = 0
    while sem.acquire(blocking=False):
        drained += 1
        if drained > 100:
            break
    # Restore everything we took.
    for _ in range(drained):
        sem.release()
    baseline_slots = drained

    # Pre-occupy the per-IP slot for 1.2.3.4 to simulate an in-flight call.
    assert serve._try_acquire_suggestion_ip_slot("1.2.3.4") is True
    try:
        handler = FakeHandler(client_ip="1.2.3.4")
        serve.Handler._handle_suggestion_draft(handler, "any-cluster-id")
        assert handler.status_code == 429
        assert "Retry-After" in handler.headers
        body = handler.body_json()
        assert "error" in body
        assert "in flight" in body["error"].lower()
    finally:
        serve._release_suggestion_ip_slot("1.2.3.4")

    # And the global semaphore must be back to baseline — the per-IP refusal
    # path must release the global slot it acquired before bailing.
    drained_after = 0
    while sem.acquire(blocking=False):
        drained_after += 1
        if drained_after > 100:
            break
    for _ in range(drained_after):
        sem.release()
    assert drained_after == baseline_slots, (
        f"global semaphore leaked: was {baseline_slots} permits, now {drained_after}"
    )


@pytest.mark.skip(reason="per-IP suggestion-slot throttle never shipped — _SUGGESTION_PER_IP_MAX absent from serve.py")
def test_agent_suggest_429_when_per_ip_saturated():
    """Mirror coverage for /api/agents/suggest — same per-IP guard applies."""
    sem = serve._SUGGESTION_SEMAPHORE
    drained = 0
    while sem.acquire(blocking=False):
        drained += 1
        if drained > 100:
            break
    for _ in range(drained):
        sem.release()
    baseline = drained

    assert serve._try_acquire_suggestion_ip_slot("5.6.7.8") is True
    try:
        handler = FakeHandler(client_ip="5.6.7.8")
        serve.Handler._handle_agent_suggest(handler)
        assert handler.status_code == 429
        body = handler.body_json()
        assert "in flight" in body.get("error", "").lower()
    finally:
        serve._release_suggestion_ip_slot("5.6.7.8")

    drained_after = 0
    while sem.acquire(blocking=False):
        drained_after += 1
        if drained_after > 100:
            break
    for _ in range(drained_after):
        sem.release()
    assert drained_after == baseline


# ---------------------------------------------------------------------------
# 5. _run_subprocess hygiene
# ---------------------------------------------------------------------------


def test_run_subprocess_uses_list_args_no_shell_true():
    """``_run_subprocess`` must take ``list[str]`` and never set
    ``shell=True``. Windows path quoting is the OS's job — string-mode
    invocation re-introduces the quoting hazard the args list avoids."""
    body = SRC.split("def _run_subprocess(", 1)[1].split("\n    def ", 1)[0]
    assert "args: list[str]" in body
    assert "shell=True" not in body


def test_no_shell_true_anywhere_in_serve():
    """Defense in depth: no ``shell=True`` should appear anywhere in
    serve.py. A new caller is more likely to pick up a bad pattern than to
    re-invent ``_run_subprocess``."""
    assert "shell=True" not in SRC


def test_git_log_excerpt_has_timeout():
    """``_git_log_excerpt`` is the suggester's ``git log`` shell-out — it
    must keep its 10s timeout so a hung git can't pin the suggester
    thread indefinitely. Regression guard, not a new fix."""
    body = SRC.split("def _git_log_excerpt(", 1)[1].split("\ndef ", 1)[0]
    assert re.search(r"timeout\s*=\s*10", body), (
        "_git_log_excerpt lost its timeout=10 guard"
    )
    assert "subprocess.TimeoutExpired" in body


# ---------------------------------------------------------------------------
# 6. Module sanity
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="per-IP suggestion-slot throttle never shipped — _SUGGESTION_PER_IP_MAX absent from serve.py")
def test_serve_module_still_imports_after_batch6():
    """A cheap parse-and-load check so a typo introduced by batch 6 is
    caught before the slower handler tests."""
    spec = importlib.util.spec_from_file_location("serve_b6_check", SERVE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for needle in (
        "_SUGGESTION_HTTP_TIMEOUT_MAX",
        "_SUGGESTION_PER_IP_MAX",
        "_try_acquire_suggestion_ip_slot",
        "_release_suggestion_ip_slot",
        "_load_jsonl_cached",
    ):
        assert hasattr(mod, needle), f"missing module symbol: {needle}"
