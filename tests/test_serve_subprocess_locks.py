"""Tests for the subprocess + concurrency guardrails added to serve.py:

* ``_WORKFLOW_UPDATE_LOCK`` — non-blocking guard around
  ``_handle_workflow_update`` so two clients can't run ``update-workflow.sh``
  against the same tree concurrently.
* ``_SUGGESTION_SEMAPHORE`` — global cap (N=2) shared by
  ``_handle_suggestion_draft`` and ``_handle_agent_suggest`` so the long
  ``claude -p`` / ``codex`` subprocess can't pin every request thread.

These tests exercise the *lock-acquire* branches only (the 409 / 429 paths).
They never actually start a subprocess, kill a server, or mutate git.
"""

from __future__ import annotations

import inspect
import io
import json
import pathlib
import re
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"))
import serve  # noqa: E402 — sys.path tweak above is the import setup


# --- FakeHandler --------------------------------------------------------------
#
# Minimal stand-in for serve.Handler so we can invoke the bound methods without
# wiring a real HTTP socket. Captures status + headers + body for assertions.
class FakeHandler:
    def __init__(self) -> None:
        self.status_code: int | None = None
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()
        self._ended = False

    def send_response(self, code: int) -> None:
        self.status_code = code

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = str(value)

    def end_headers(self) -> None:
        self._ended = True

    # _json(status, payload) is what most handlers call. Mirror serve.Handler._json.
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


# --- _WORKFLOW_UPDATE_LOCK ----------------------------------------------------

def test_workflow_update_lock_exists() -> None:
    """The lock must exist and be a threading.Lock instance."""
    assert hasattr(serve, "_WORKFLOW_UPDATE_LOCK"), \
        "serve._WORKFLOW_UPDATE_LOCK was not defined"
    # threading.Lock() returns a private _thread.lock instance; both have
    # acquire/release. Probe the duck-type instead of the class.
    lock = serve._WORKFLOW_UPDATE_LOCK
    assert hasattr(lock, "acquire") and hasattr(lock, "release"), \
        "_WORKFLOW_UPDATE_LOCK does not look like a lock"
    assert callable(lock.acquire) and callable(lock.release)
    # Sanity: acquire/release cycle works.
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_workflow_update_lock_acquire_returns_409() -> None:
    """When the lock is already held, _handle_workflow_update must respond
    409 without doing any work (no clone, no subprocess).

    We hold the lock from the test thread to simulate a second client racing
    the first. The handler should NOT spawn anything — it should bail out at
    the lock check.
    """
    handler = FakeHandler()
    lock = serve._WORKFLOW_UPDATE_LOCK

    # Pre-acquire so the handler's own acquire(blocking=False) fails.
    acquired = lock.acquire(blocking=False)
    assert acquired, "could not pre-acquire the lock (held by another test?)"
    try:
        serve.Handler._handle_workflow_update(handler)
    finally:
        lock.release()

    assert handler.status_code == 409, \
        f"expected 409 when lock held, got {handler.status_code}"
    body = handler.body_json()
    assert "error" in body
    assert "in progress" in body["error"].lower()


# --- _SUGGESTION_SEMAPHORE ----------------------------------------------------

def test_suggestion_semaphore_exists() -> None:
    """The semaphore must exist as a module-level attribute."""
    assert hasattr(serve, "_SUGGESTION_SEMAPHORE"), \
        "serve._SUGGESTION_SEMAPHORE was not defined"
    sem = serve._SUGGESTION_SEMAPHORE
    assert hasattr(sem, "acquire") and hasattr(sem, "release"), \
        "_SUGGESTION_SEMAPHORE does not look like a semaphore"
    # Probe acquire+release cycle to confirm it's actually usable.
    assert sem.acquire(blocking=False) is True
    sem.release()


def _drain_semaphore(sem) -> int:
    """Acquire the semaphore until it blocks, return the count drained.

    We use blocking=False to avoid hanging if the semaphore was already drained
    by leftover state from a previous test.
    """
    drained = 0
    while sem.acquire(blocking=False):
        drained += 1
        # Hard ceiling so a buggy fix that turns this into an unbounded
        # semaphore doesn't loop forever in CI.
        if drained > 100:
            break
    return drained


def test_suggestion_semaphore_returns_429_on_saturation() -> None:
    """When the semaphore is fully drained, both endpoints must respond 429
    with a Retry-After header instead of running the subprocess."""
    sem = serve._SUGGESTION_SEMAPHORE
    drained = _drain_semaphore(sem)
    assert drained >= 1, "expected to drain at least one permit"
    try:
        # _handle_suggestion_draft
        h1 = FakeHandler()
        serve.Handler._handle_suggestion_draft(h1, "any-cluster-id")
        assert h1.status_code == 429, \
            f"_handle_suggestion_draft: expected 429, got {h1.status_code}"
        assert "Retry-After" in h1.headers, \
            "_handle_suggestion_draft: missing Retry-After header on 429"
        body1 = h1.body_json()
        assert "error" in body1

        # _handle_agent_suggest
        h2 = FakeHandler()
        serve.Handler._handle_agent_suggest(h2)
        assert h2.status_code == 429, \
            f"_handle_agent_suggest: expected 429, got {h2.status_code}"
        assert "Retry-After" in h2.headers, \
            "_handle_agent_suggest: missing Retry-After header on 429"
        body2 = h2.body_json()
        assert "error" in body2
    finally:
        # Replenish what we drained so other tests / the real server aren't
        # left with a permanently saturated semaphore.
        for _ in range(drained):
            sem.release()


def _serve_function_source(name: str) -> str:
    # Follow the re-export: serve.<name> may now live in a server/* module
    # (e.g. _persist_job moved to server.jobs.persistence). inspect.getsource
    # reads from the function's defining module, so the file-lock structure
    # check works regardless of which module physically owns the function.
    return inspect.getsource(getattr(serve, name))


def _assert_file_lock_block(function_name: str, lock_name: str, file_name: str) -> None:
    src = _serve_function_source(function_name)
    assert re.search(
        rf"with {lock_name}:.*?with {file_name}\.open\(\"a\", encoding=\"utf-8\"\) as f:",
        src,
        re.DOTALL,
    )
    assert re.search(r"fcntl\.flock", src)
    assert re.search(r"fcntl\.LOCK_EX", src)
    assert re.search(r"fcntl\.LOCK_UN", src)
    assert re.search(r"msvcrt\.locking", src)
    assert re.search(r"msvcrt\.LK_LOCK", src)
    assert re.search(r"msvcrt\.LK_UNLCK", src)


def test_persist_job_uses_file_lock() -> None:
    _assert_file_lock_block("_persist_job", "_JOBS_PERSIST_LOCK", "JOBS_PERSIST_FILE")


def test_audit_improvement_uses_file_lock() -> None:
    _assert_file_lock_block("_audit_improvement", "_IMPROVEMENTS_LEDGER_LOCK", "IMPROVEMENTS_LEDGER")


def test_record_skill_metrics_uses_file_lock() -> None:
    _assert_file_lock_block("_record_skill_metrics", "_SKILL_METRICS_LOCK", "SKILL_METRICS_FILE")
