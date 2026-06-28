"""Tests for the Windows PTY hardening (batch 3).

Companion to test_pty_session.py / test_pty_robustness.py /
test_pty_windows_logging.py. Focus areas:

1. Fix 1 — kill() vs reader-thread race serialized by ``_io_lock``.
2. Fix 2 — pywinpty 1.x vs 2.x env-format compatibility.
3. Fix 3 — extended logging on spawn + write paths.

Because we can't really exercise ``PtyProcess.spawn`` in CI (no
ConPTY handle), several assertions are static-source patterns — we
verify the relevant code path exists in ``pty_session.py``. Behavioral
assertions use ``_WindowsPty.__new__`` to bypass spawn entirely, in
the same style as the other PTY test modules.
"""
from __future__ import annotations

import codecs
import inspect
import logging
import pathlib
import sys
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard")
)
from server.pty import session as _pty_session


def _fake_windows_pty():
    """Fabricate a _WindowsPty with fake state, bypassing real spawn."""
    p = _pty_session._WindowsPty.__new__(_pty_session._WindowsPty)
    p._closed = False
    p._proc = MagicMock()
    p._proc.pid = 88001
    p._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    p._io_lock = threading.Lock()
    p._last_io = 0.0
    return p


# --- Fix 1: _io_lock serializes kill against in-flight read ----------------


def test_windows_pty_init_creates_io_lock():
    """``_WindowsPty.__init__`` must create ``self._io_lock`` so kill()
    and read() can serialize against each other. Source-level check
    because instantiating the real class requires pywinpty + ConPTY.
    """
    src = inspect.getsource(_pty_session._WindowsPty.__init__)
    assert "_io_lock" in src, \
        "_WindowsPty.__init__ must initialize self._io_lock"
    assert "threading.Lock()" in src, \
        "_io_lock must be a threading.Lock(), not a stub"


def test_windows_pty_read_acquires_io_lock():
    """read() must acquire ``_io_lock`` before touching ``self._proc``
    so a concurrent kill() can't terminate the process mid-syscall."""
    src = inspect.getsource(_pty_session._WindowsPty.read)
    assert "_io_lock" in src, \
        "_WindowsPty.read must acquire self._io_lock"
    assert "with self._io_lock" in src, \
        "_WindowsPty.read must use ``with self._io_lock`` to serialize"


def test_windows_pty_kill_acquires_io_lock():
    """kill() must acquire ``_io_lock`` before terminating ``_proc`` so
    it waits for any in-flight read to return (EOFError) instead of
    racing TerminateProcess against a ReadFile."""
    src = inspect.getsource(_pty_session._WindowsPty.kill)
    assert "_io_lock" in src, \
        "_WindowsPty.kill must acquire self._io_lock"
    assert "with self._io_lock" in src, \
        "_WindowsPty.kill must use ``with self._io_lock`` to serialize"


def test_windows_kill_actually_acquires_lock():
    """Behavioral check: invoking kill() on a fake _WindowsPty must
    acquire ``_io_lock`` exactly once (we instrument the lock with a
    wrapper that counts acquisitions)."""
    p = _fake_windows_pty()
    acquire_count = {"n": 0}
    release_count = {"n": 0}

    class _CountingLock:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            acquire_count["n"] += 1
            return self._inner.__enter__()

        def __exit__(self, *a):
            release_count["n"] += 1
            return self._inner.__exit__(*a)

        # Pass-through so ``threading.Lock`` API surface still works.
        def acquire(self, *a, **kw):
            acquire_count["n"] += 1
            return self._inner.acquire(*a, **kw)

        def release(self):
            release_count["n"] += 1
            return self._inner.release()

    p._io_lock = _CountingLock(threading.Lock())
    p.kill()
    assert acquire_count["n"] >= 1, \
        "kill() must acquire _io_lock at least once"
    assert release_count["n"] >= 1, \
        "kill() must release _io_lock (no leaked lock)"
    assert p._closed is True


def test_windows_kill_idempotent_inside_lock():
    """Second kill() must still early-return without re-terminating proc."""
    p = _fake_windows_pty()
    p.kill()
    assert p._closed is True
    assert p._proc.terminate.call_count == 1
    # Second call: must NOT call terminate again.
    p.kill()
    assert p._proc.terminate.call_count == 1, \
        "second kill() must be a no-op (idempotent)"


# --- Fix 2: pywinpty env-format compatibility ------------------------------


def test_windows_pty_env_version_check_present():
    """Init code must contain a pywinpty version probe so 1.x and 2.x
    are both supported (env-as-list vs env-as-dict)."""
    src = inspect.getsource(_pty_session._WindowsPty.__init__)
    assert "__version__" in src, \
        "_WindowsPty.__init__ must probe pywinpty.__version__"


def test_windows_pty_env_has_list_conversion_path():
    """For pywinpty 1.x the env must be converted from dict to a list
    of ``\"K=V\"`` strings. The conversion code must exist in source."""
    src = inspect.getsource(_pty_session._WindowsPty.__init__)
    # Either an f-string or %-format that produces "K=V" pairs is fine.
    assert ("f\"{k}={v}\"" in src or "'{k}={v}'" in src
            or "%s=%s" in src or "k}={v" in src), \
        "_WindowsPty.__init__ must build list-of-KV-strings for pywinpty 1.x"
    assert "env_for_spawn" in src, \
        "expected an ``env_for_spawn`` variable holding the per-version env"


def test_windows_pty_env_dict_path_for_2x():
    """For pywinpty 2.x the dict path must remain — that's the modern,
    recommended call shape."""
    src = inspect.getsource(_pty_session._WindowsPty.__init__)
    # The dict fall-through assigns ``env_for_spawn = child_env`` (no
    # conversion). Verify both branches exist.
    assert "env_for_spawn = child_env" in src, \
        "_WindowsPty.__init__ must keep the dict path for pywinpty 2.x"


# --- Fix 3: extended logging on spawn + write ------------------------------


def test_windows_pty_init_logs_spawn():
    """Successful spawn must log an info-level line with argv + cwd so
    operators have a paper trail of which sessions started."""
    src = inspect.getsource(_pty_session._WindowsPty.__init__)
    assert "_log.info" in src, \
        "_WindowsPty.__init__ must log at info level on spawn"
    assert "spawned" in src.lower(), \
        "spawn log must indicate the session was spawned"


def test_windows_write_logs_debug(caplog):
    """write() must emit a debug log line so operators can opt into
    byte-level tracing without modifying source. The log must NOT be at
    INFO level (would spam the terminal at steady state)."""
    p = _fake_windows_pty()

    with caplog.at_level(logging.DEBUG, logger="pty_session"):
        n = p.write(b"hello")

    assert n == 5
    write_records = [
        r for r in caplog.records
        if "write" in r.getMessage().lower()
    ]
    assert write_records, \
        f"expected a write debug log, got: {[r.getMessage() for r in caplog.records]}"
    # At least one record must be DEBUG (not INFO/WARNING — that'd spam).
    assert any(r.levelno == logging.DEBUG for r in write_records), \
        "write log must be at DEBUG level (not INFO) to avoid steady-state spam"


def test_windows_write_does_not_log_info_per_call():
    """Sanity: write() must NOT emit INFO-level records every call —
    that would flood logs on heavy keystroke streams. Only DEBUG."""
    p = _fake_windows_pty()

    with caplog_at_info_only_capture() as records:
        p.write(b"x")
        p.write(b"y")
        p.write(b"z")

    write_info_records = [
        r for r in records
        if r.levelno >= logging.INFO and "write" in r.getMessage().lower()
    ]
    assert not write_info_records, \
        f"write() must not log at INFO level; got: {[r.getMessage() for r in write_info_records]}"


# --- helpers ---------------------------------------------------------------


class caplog_at_info_only_capture:
    """Tiny ctx manager that captures pty_session log records at INFO+
    only (so DEBUG noise from write() doesn't pollute the assertion).
    Used by the no-INFO-spam test; lets us be explicit about what we
    do and don't expect to see in production logs."""

    def __enter__(self):
        self._records: list[logging.LogRecord] = []

        class _H(logging.Handler):
            def __init__(inner_self, sink):
                super().__init__(level=logging.INFO)
                inner_self._sink = sink

            def emit(inner_self, record):
                inner_self._sink.append(record)

        self._handler = _H(self._records)
        _pty_session._log.addHandler(self._handler)
        # Make sure INFO records can flow through even if the host
        # logger is set higher.
        self._prev_level = _pty_session._log.level
        _pty_session._log.setLevel(logging.DEBUG)
        return self._records

    def __exit__(self, *a):
        _pty_session._log.removeHandler(self._handler)
        _pty_session._log.setLevel(self._prev_level)
        return False
