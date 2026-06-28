"""Robustness tests for pty_session.py.

Companion to test_pty_session.py; focuses on the hardening fixes added
for short-write looping, errno-distinguishing reads, alias safety, max-
session enforcement, and PATH-injection resistance.

All tests are mock-based and platform-agnostic — they instantiate
``_PosixPty`` via ``__new__`` so no real ``pty.fork`` is ever performed.
"""
from __future__ import annotations

import errno
import os
import pathlib
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
from server.pty import session as _pty_session


def _fake_posix_pty():
    """Create a _PosixPty instance with fake state; bypasses real fork."""
    p = _pty_session._PosixPty.__new__(_pty_session._PosixPty)
    p._closed = False
    p._pid = 99001
    p._fd = 7
    p._last_io = 0.0
    return p


# --- Bug 2: os.write short-write looping ---------------------------------


def test_short_write_loops():
    """write() must loop until all bytes are flushed, not trust a single
    os.write return value. Otherwise keystrokes silently disappear."""
    p = _fake_posix_pty()
    payload = b"hello world"
    # First call writes 4 bytes, second writes the remaining 7.
    write_calls = []

    def fake_write(fd, data):
        write_calls.append(bytes(data))
        if len(write_calls) == 1:
            return 4
        return len(data)

    with patch.object(os, "write", side_effect=fake_write):
        n = p.write(payload)

    assert n == len(payload), f"write() returned short total: {n}"
    assert len(write_calls) >= 2, "write() did not loop on short return"
    # Sum of underlying writes equals payload length.
    total_underlying = sum(len(c) for c in write_calls)
    # The slice may differ in repr; total length is what matters.
    assert total_underlying >= len(payload), \
        "underlying os.write didn't see all bytes"
    assert p._closed is False


def test_write_eof_returns_partial():
    """If os.write returns 0 mid-loop, treat as EOF: flip _closed,
    return the bytes flushed so far (not silently spinning)."""
    p = _fake_posix_pty()
    payload = b"abcdef"
    seq = iter([3, 0])  # 3 bytes flushed, then EOF.

    with patch.object(os, "write", side_effect=lambda fd, d: next(seq)):
        n = p.write(payload)

    assert n == 3, f"expected partial 3, got {n}"
    assert p._closed is True, "EOF on write should close the pty"


# --- Bug 3: EAGAIN distinguished from EOF --------------------------------


def test_eagain_distinguished_from_eof():
    """OSError with errno EAGAIN/EWOULDBLOCK is 'no data right now',
    NOT a legitimate EOF — must NOT flip _closed."""
    p = _fake_posix_pty()

    with patch.object(
        os, "read", side_effect=OSError(errno.EAGAIN, "again")
    ):
        data = p.read(4096)

    assert data == b"", "EAGAIN should return empty bytes"
    assert p._closed is False, \
        "EAGAIN must not close the pty (caller should retry)"


def test_ewouldblock_distinguished_from_eof():
    p = _fake_posix_pty()

    with patch.object(
        os, "read", side_effect=OSError(errno.EWOULDBLOCK, "would block")
    ):
        data = p.read(4096)

    assert data == b""
    assert p._closed is False


def test_eio_treated_as_eof():
    """EIO is Linux's signal that the slave side closed — legitimate EOF."""
    p = _fake_posix_pty()

    with patch.object(os, "read", side_effect=OSError(errno.EIO, "io")):
        data = p.read(4096)

    assert data == b""
    assert p._closed is True


def test_ebadf_treated_as_eof():
    p = _fake_posix_pty()

    with patch.object(os, "read", side_effect=OSError(errno.EBADF, "bad fd")):
        data = p.read(4096)

    assert data == b""
    assert p._closed is True


# --- Bug 4: aliases returns fresh argv list ------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only paths (/usr/local/bin/bash); _is_under_trusted_dir uses os.path.realpath which on Windows resolves them against the current drive",
)
def test_aliases_returns_fresh_list():
    """Two consecutive resolve_shell("bash") calls must each return a
    fresh list — mutating one must not leak into the next."""
    # Force the fallback path (shutil.which) to a deterministic value so
    # the absolute-path probe doesn't pick a real /bin/bash off the host.
    # Use a trusted-dir path (batch 7 hardened resolve_shell to reject
    # which() results outside _TRUSTED_SHELL_DIRS).
    fallback = "/usr/local/bin/bash"
    with patch.object(_pty_session.os.path, "exists", return_value=False), \
         patch.object(_pty_session.shutil, "which", return_value=fallback):
        a = _pty_session.resolve_shell("bash")
        a[0] = "/tmp/hijacked"
        a.append("--injected")
        b = _pty_session.resolve_shell("bash")

    assert a is not b, "resolve_shell returned the same list object twice"
    assert b[0] == fallback, \
        f"second call's bin_path was tainted by first call: {b}"
    assert "--injected" not in b, \
        f"second call inherited mutated arg from first: {b}"


# --- Bug 5: resolve_shell prefers absolute path over PATH ----------------


def test_resolve_shell_prefers_absolute():
    """When a trusted absolute path exists, resolve_shell must use it
    INSTEAD of shutil.which — protects against PATH injection where an
    attacker plants a malicious binary earlier on PATH."""
    trusted = _pty_session._TRUSTED_SHELL_PATHS["bash"][0]  # /bin/bash

    def fake_exists(p):
        return p == trusted

    which_mock = patch.object(
        _pty_session.shutil, "which", return_value="/malicious/bash"
    )
    with patch.object(_pty_session.os.path, "exists", side_effect=fake_exists), \
         which_mock as m:
        argv = _pty_session.resolve_shell("bash")

    assert argv[0] == trusted, \
        f"expected trusted absolute path, got {argv[0]!r}"
    m.assert_not_called()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only paths (/opt/homebrew/bin); _is_under_trusted_dir's realpath rewrites them on Windows",
)
def test_resolve_shell_falls_back_to_which():
    """If NO trusted absolute path exists, fall back to shutil.which so
    nix-store / brew / windows-without-system32 layouts still work.
    Batch 7 hardened the fallback: which() result must live under
    _TRUSTED_SHELL_DIRS (homebrew canonical path /opt/homebrew/bin/)."""
    with patch.object(_pty_session.os.path, "exists", return_value=False), \
         patch.object(
             _pty_session.shutil, "which", return_value="/opt/homebrew/bin/bash"
         ):
        argv = _pty_session.resolve_shell("bash")

    assert argv[0] == "/opt/homebrew/bin/bash"


# --- Bug 6: max sessions enforced ----------------------------------------


def test_max_sessions_enforced(monkeypatch):
    """Once active_count reaches MAX_PTY_SESSIONS, spawn must raise
    RuntimeError. (serve.py maps that to HTTP 503.)

    Platform-agnostic: we monkeypatch BOTH concrete Pty subclasses to a
    no-op fake so the test works on Windows (no termios) and POSIX alike.
    """
    monkeypatch.setattr(_pty_session, "MAX_PTY_SESSIONS", 2)
    # Clear the registry under the lock to give the test a known baseline.
    with _pty_session.Pty._registry_lock:
        _pty_session.Pty._registry.clear()

    class _FakePty(_pty_session.Pty):
        _counter = 0

        def __init__(self, argv, cwd, env, cols, rows):
            _FakePty._counter += 1
            self._pid = 1000 + _FakePty._counter
            self._closed = False
            self._last_io = 0.0

        def read(self, n=4096):
            return b""

        def write(self, data):
            return len(data)

        def resize(self, cols, rows):
            pass

        def kill(self):
            self._closed = True

        def alive(self):
            return not self._closed

        @property
        def pid(self):
            return self._pid

    # Patch BOTH platform paths so spawn picks our fake regardless of host.
    monkeypatch.setattr(_pty_session, "_PosixPty", _FakePty)
    monkeypatch.setattr(_pty_session, "_WindowsPty", _FakePty)

    try:
        # Two should succeed.
        a = _pty_session.Pty.spawn(["/bin/bash"], cwd=None, env={}, cols=80, rows=24)
        b = _pty_session.Pty.spawn(["/bin/bash"], cwd=None, env={}, cols=80, rows=24)
        assert _pty_session.Pty.active_count() == 2
        # Third must hit the cap.
        with pytest.raises(RuntimeError, match="max PTY sessions"):
            _pty_session.Pty.spawn(
                ["/bin/bash"], cwd=None, env={}, cols=80, rows=24
            )
        # After killing one, a new spawn should succeed again.
        a.kill()
        _pty_session.Pty._unregister(a)
        c = _pty_session.Pty.spawn(["/bin/bash"], cwd=None, env={}, cols=80, rows=24)
        assert c is not None
    finally:
        with _pty_session.Pty._registry_lock:
            _pty_session.Pty._registry.clear()


# --- Bonus: idle cleanup -------------------------------------------------


def test_cleanup_idle_kills_stale():
    """cleanup_idle() must kill PTYs whose last_io is older than the
    timeout and unregister them; fresh ones survive."""
    with _pty_session.Pty._registry_lock:
        _pty_session.Pty._registry.clear()

    stale = _fake_posix_pty()
    stale._last_io = 0.0  # ancient
    fresh = _fake_posix_pty()
    fresh._pid = 99002
    # Register both manually (avoid spawn path).
    _pty_session.Pty._register(stale)
    _pty_session.Pty._register(fresh)
    # _register stamps last_io to now; reset stale to ancient.
    stale._last_io = 0.0

    kill_calls = []

    def fake_kill(self):
        kill_calls.append(self._pid)
        self._closed = True

    with patch.object(_pty_session._PosixPty, "kill", fake_kill):
        victims = _pty_session.Pty.cleanup_idle(timeout=60.0)

    assert kill_calls == [stale._pid], \
        f"expected only stale pty killed; got {kill_calls}"
    assert any(v is stale for v in victims)
    # Fresh pty still registered.
    with _pty_session.Pty._registry_lock:
        live_ids = set(_pty_session.Pty._registry.keys())
    assert id(fresh) in live_ids
    assert id(stale) not in live_ids

    # Cleanup for test isolation.
    with _pty_session.Pty._registry_lock:
        _pty_session.Pty._registry.clear()
