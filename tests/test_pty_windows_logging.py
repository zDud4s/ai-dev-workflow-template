"""Tests for the logging / Windows / idempotency hardening of pty_session.

Mock-based and platform-agnostic (consistent with test_pty_session.py and
test_pty_robustness.py): we never actually fork a child or open a real
PTY. All tests instantiate ``_PosixPty`` via ``__new__`` so they run on
Windows CI hosts too.
"""
from __future__ import annotations

import codecs
import logging
import os
import pathlib
import re
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"))
from server import pty_session as _pty_session

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_EVENT_PATH = ROOT / ".ai" / "dashboard" / "scripts" / "log_event.py"


def _fake_posix_pty():
    """Bypass real pty.fork — fabricate a _PosixPty with fake state."""
    p = _pty_session._PosixPty.__new__(_pty_session._PosixPty)
    p._closed = False
    p._pid = 99001
    p._fd = -1
    p._last_io = 0.0
    return p


# --- Fix 1: module has a logger -----------------------------------------


def test_module_has_logger():
    """pty_session exposes a module-level ``_log`` so operators can wire
    handlers without having to spelunk for the right logger name."""
    assert hasattr(_pty_session, "_log"), \
        "pty_session must expose a module-level _log"
    assert isinstance(_pty_session._log, logging.Logger), \
        f"_log must be a logging.Logger, got {type(_pty_session._log)!r}"
    assert _pty_session._log.name == "pty_session"


# --- Fix 2: kill() is idempotent ----------------------------------------


def test_kill_is_idempotent():
    """A second kill() on an already-closed PTY must NOT touch os.close
    or os.kill — otherwise we silently swallow EBADF on the closed fd
    and mask real bugs. Idempotency must be by design (early return on
    ``_closed``), not by accident (caught exception)."""
    p = _fake_posix_pty()
    p._closed = True  # simulate already-killed PTY

    with patch.object(os, "close") as close_mock, \
         patch.object(os, "kill") as kill_mock:
        p.kill()

    close_mock.assert_not_called()
    kill_mock.assert_not_called()


def test_kill_logs_sigkill_escalation(caplog):
    """When SIGTERM doesn't reap the child within the timeout window,
    kill() must escalate to SIGKILL AND emit a warning log so the
    operator has a paper trail of which sessions misbehaved."""
    import signal as _signal

    p = _fake_posix_pty()

    # _reap_with_timeout returns False both times (never reaps). os.kill
    # is a no-op so the SIGKILL path doesn't error. os.close is also a
    # no-op (fd is -1 in the fake). On Windows, ``signal.SIGKILL``
    # doesn't exist — inject a fake value so the kill() code path can
    # still be exercised platform-agnostically.
    with patch.object(_pty_session._PosixPty, "_reap_with_timeout",
                      return_value=False), \
         patch.object(_signal, "SIGKILL", 9, create=True), \
         patch.object(_signal, "SIGTERM", 15, create=True), \
         patch.object(os, "kill"), \
         patch.object(os, "close"), \
         caplog.at_level(logging.WARNING, logger="pty_session"):
        p.kill()

    sigkill_records = [
        r for r in caplog.records
        if "SIGKILL" in r.getMessage()
    ]
    assert sigkill_records, \
        f"expected SIGKILL warning in logs, got: {[r.getMessage() for r in caplog.records]}"
    assert sigkill_records[0].levelno == logging.WARNING


# --- Fix 3: Windows resize logs on failure ------------------------------


def test_windows_resize_logs_on_exception(caplog):
    """The Windows resize() used to be a bare ``except: pass``. After
    the fix it still swallows the error (best-effort: ConPTY's resize
    can legitimately fail during teardown) but now logs a warning so we
    have a diagnostic if resize starts failing systematically."""
    p = _pty_session._WindowsPty.__new__(_pty_session._WindowsPty)
    p._closed = False
    p._proc = MagicMock()
    p._proc.setwinsize.side_effect = RuntimeError("conpty boom")
    p._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    with caplog.at_level(logging.WARNING, logger="pty_session"):
        # Must not raise — best-effort by design.
        p.resize(cols=120, rows=30)

    resize_records = [
        r for r in caplog.records
        if "resize" in r.getMessage().lower()
    ]
    assert resize_records, \
        f"expected a resize warning in logs, got: {[r.getMessage() for r in caplog.records]}"


# --- Fix 4: read() proactively reaps on EOF -----------------------------


def test_read_eof_reaps_proactively():
    """When os.read returns empty bytes (EOF — child closed its PTY
    end), read() must proactively call waitpid so we don't leave a
    zombie sitting around until the next alive()/kill() call."""
    p = _fake_posix_pty()

    # os.read returns b"" → EOF path. waitpid records that it was
    # called; returning (pid, 0) means "reaped successfully", so the
    # _reap_with_timeout loop exits on the first iteration.
    # NB: os.WNOHANG doesn't exist on Windows — patch it in so this
    # test stays platform-agnostic (we never actually fork here).
    with patch.object(os, "read", return_value=b""), \
         patch.object(os, "WNOHANG", 1, create=True), \
         patch.object(os, "waitpid", return_value=(p._pid, 0)) as wp_mock:
        data = p.read(4096)

    assert data == b"", "EOF read must return empty bytes"
    assert p._closed is True, "EOF read must flip _closed"
    assert wp_mock.call_count >= 1, \
        "EOF read must proactively call waitpid (no zombie)"


def test_log_event_binary_append_fixed_offset_lock():
    text = LOG_EVENT_PATH.read_text(encoding="utf-8")

    assert 'EVENTS_FILE.open("ab")' in text
    assert re.search(
        r"def _msvcrt_lock_at_start\(f, msvcrt, mode\).*?"
        r"f\.seek\(0\).*?msvcrt\.locking\(f\.fileno\(\), mode, 1\)",
        text,
        flags=re.DOTALL,
    )
    assert "_msvcrt_lock_at_start(f, msvcrt, msvcrt.LK_LOCK)" in text
    assert "_msvcrt_lock_at_start(f, msvcrt, msvcrt.LK_UNLCK)" in text

    lock_pos = text.index("msvcrt.LK_LOCK")
    unlock_pos = text.index("msvcrt.LK_UNLCK")
    assert "seek(0)" not in text[lock_pos:unlock_pos]
