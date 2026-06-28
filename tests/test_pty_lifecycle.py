"""Lifecycle + docstring + logging hardening tests for pty_session.py.

These tests complement the existing pty_session/pty_robustness suites by
exercising:

  1. ``_WindowsPty.__init__`` lifecycle hole — if ``PtyProcess.spawn``
     raises, no half-initialized state escapes; if a downstream step
     (decoder build) raises, the already-spawned ``_proc`` is torn down
     before the exception propagates.
  2. Public abstract methods on ``Pty`` have non-empty docstrings.
  3. ``resolve_shell`` docstring documents the PATH-injection hardening.
  4. ``cleanup_idle`` logs (rather than silently swallows) kill failures.
  5. ``_WindowsPty.resize`` logs with ``exc_info=True`` so a recurring
     ConPTY rejection leaves a traceback for diagnosis.

All tests are platform-agnostic: we bypass real PTY/fork by
``__new__``-ing instances and patching the underlying ``PtyProcess`` or
``os`` calls. Safe to run on a host without pywinpty / posix pty.
"""
from __future__ import annotations

import codecs
import inspect
import logging
import os
import pathlib
import re
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"),
)
from server import pty_session as _pty_session  # noqa: E402
import serve  # noqa: E402
import server.pty as _server_pty  # noqa: E402 — PTY state/funcs moved here; serve re-exports


_PTY_SOURCE_PATH = pathlib.Path(_pty_session.__file__)
_PTY_SOURCE = _PTY_SOURCE_PATH.read_text(encoding="utf-8")
_SERVE_SOURCE_PATH = pathlib.Path(serve.__file__)
_SERVE_SOURCE = _SERVE_SOURCE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def clean_serve_ptys():
    with serve.PTYS_LOCK:
        saved_ptys = dict(serve.PTYS)
        serve.PTYS.clear()
    with _pty_session.Pty._registry_lock:
        saved_registry = dict(_pty_session.Pty._registry)
        _pty_session.Pty._registry.clear()
    try:
        yield
    finally:
        with serve.PTYS_LOCK:
            serve.PTYS.clear()
            serve.PTYS.update(saved_ptys)
        with _pty_session.Pty._registry_lock:
            _pty_session.Pty._registry.clear()
            _pty_session.Pty._registry.update(saved_registry)


@pytest.fixture
def fake_posix_kill():
    def fake_waitpid(pid, flags):  # noqa: ARG001
        return (pid, 0)

    with patch.object(os, "WNOHANG", 1, create=True), \
         patch.object(os, "kill"), \
         patch.object(os, "waitpid", side_effect=fake_waitpid, create=True), \
         patch.object(os, "close"):
        yield


_NEXT_FAKE_PID = 1000


def _fake_posix_pty() -> _pty_session._PosixPty:
    global _NEXT_FAKE_PID
    _NEXT_FAKE_PID += 1
    p = _pty_session._PosixPty.__new__(_pty_session._PosixPty)
    p._closed = False
    p._pid = _NEXT_FAKE_PID
    p._fd = -1
    _pty_session.Pty._register(p)
    return p


def _add_lifecycle_entry(
    pty_id: str,
    status: str,
    created_at: str,
) -> _pty_session._PosixPty:
    pty = _fake_posix_pty()
    with serve.PTYS_LOCK:
        serve.PTYS[pty_id] = {
            "id": pty_id,
            "kind": "terminal",
            "shell": "test",
            "argv": ["test"],
            "cwd": str(serve.ROOT),
            "cols": 80,
            "rows": 24,
            "pid": pty.pid,
            "created_at": created_at,
            "status": status,
            "exit_code": None,
            "_pty": pty,
            "_ring": bytearray(),
            "_subscribers": [],
            "_lock": threading.Lock(),
            "_token": "test-token",
        }
    return pty


# ---------- Static (regex) assertions ----------

def test_no_bare_except_in_pty_session():
    """``except:`` with no class is a code smell — it silently catches
    BaseException including SystemExit/KeyboardInterrupt. The fix is
    ``except Exception:`` or a specific class. Regex matches only the
    bare form (``except:`` followed by newline/whitespace, not
    ``except Exception:``, etc.)."""
    # Find any "except" not followed by a class name, then ":".
    # Pattern: 'except' + optional whitespace + ':' (no class between).
    bare = re.findall(r"^\s*except\s*:\s*$", _PTY_SOURCE, flags=re.MULTILINE)
    assert not bare, (
        f"Found {len(bare)} bare `except:` block(s) in pty_session.py — "
        f"replace with `except Exception:` or a specific class"
    )


def test_windows_init_has_lifecycle_guard():
    """``_WindowsPty.__init__`` must wrap ``PtyProcess.spawn`` in a
    try/except that cleans up a partial ``_proc`` before re-raising —
    otherwise a downstream failure inside __init__ would leak a ConPTY
    child."""
    src = inspect.getsource(_pty_session._WindowsPty.__init__)
    # The init body must include `PtyProcess.spawn(` AND a `try:` block
    # surrounding it (we look for the spawn call between a `try:` and
    # an `except`).
    assert "PtyProcess.spawn(" in src
    # Locate try/except structure
    try_idx = src.find("try:")
    spawn_idx = src.find("PtyProcess.spawn(")
    except_idx = src.find("except Exception", try_idx if try_idx >= 0 else 0)
    raise_idx = src.find("raise", except_idx if except_idx >= 0 else 0)
    assert try_idx >= 0, "No `try:` in _WindowsPty.__init__"
    assert spawn_idx > try_idx, (
        "PtyProcess.spawn must be wrapped in the try-block"
    )
    assert except_idx > spawn_idx, (
        "An `except Exception` must follow the spawn try-block"
    )
    assert raise_idx > except_idx, (
        "The except block must re-raise after cleaning up"
    )
    # Must mention terminate to confirm cleanup path exists.
    assert "terminate" in src, (
        "Cleanup path must call terminate() to kill a stranded ConPTY child"
    )


def test_resize_uses_exc_info():
    """The Windows resize() warning must capture the traceback via
    ``exc_info=True`` so operators can diagnose recurring failures."""
    src = inspect.getsource(_pty_session._WindowsPty.resize)
    assert "exc_info=True" in src, (
        "_WindowsPty.resize must log with exc_info=True for traceback capture"
    )


def test_cleanup_idle_logs_kill_failures():
    """``cleanup_idle`` must log (not silently swallow) ``kill()``
    failures so a misbehaving PtyProcess leaves a paper trail."""
    src = inspect.getsource(_pty_session.Pty.cleanup_idle)
    # Source must mention _log.* inside the except block (not just pass).
    assert "_log.warning" in src or "_log.error" in src, (
        "cleanup_idle must log kill() failures (warning/error level)"
    )
    assert "exc_info=True" in src, (
        "cleanup_idle kill-failure log must include exc_info=True"
    )


def test_resolve_shell_docstring_mentions_path_injection():
    """The resolve_shell docstring must explicitly document the
    PATH-injection hardening (trusted abs paths before shutil.which) so
    future maintainers understand the security invariant."""
    doc = _pty_session.resolve_shell.__doc__ or ""
    low = doc.lower()
    assert "path" in low and "injection" in low, (
        "resolve_shell docstring must mention PATH-injection"
    )
    assert "trust" in low or "whitelist" in low or "allowlist" in low, (
        "resolve_shell docstring must explain the trust boundary"
    )


# ---------- Public abstract method docstrings ----------

@pytest.mark.parametrize(
    "method_name",
    ["read", "write", "resize", "kill", "alive", "pid"],
)
def test_abstract_methods_have_docstrings(method_name):
    """Each abstract method/property on the Pty ABC must carry a
    non-trivial docstring (>20 chars). The previous template raised
    NotImplementedError without explaining the contract."""
    attr = getattr(_pty_session.Pty, method_name)
    # For property, look at fget's docstring; abstractmethod preserves __doc__.
    doc = attr.__doc__ or ""
    assert len(doc.strip()) > 20, (
        f"Pty.{method_name}.__doc__ is empty or trivially short: "
        f"{doc!r}"
    )


def test_no_magic_max_sessions_inline():
    """``MAX_PTY_SESSIONS`` is a module constant so dashboards can tune
    it without editing class bodies. The class body must reference the
    constant rather than hard-coding a magic number."""
    src = inspect.getsource(_pty_session.Pty.spawn)
    assert "MAX_PTY_SESSIONS" in src, (
        "Pty.spawn must reference the MAX_PTY_SESSIONS constant"
    )
    # No raw integer comparison `>= 32` inside spawn (magic number).
    assert ">= 32" not in src, (
        "Pty.spawn appears to hard-code the session cap as 32 — "
        "use the MAX_PTY_SESSIONS constant instead"
    )


# ---------- Functional behavior tests ----------

def _fake_winpty():
    """Create a _WindowsPty instance with fake state — bypasses real
    ConPTY spawn so we can exercise method paths on any host."""
    p = _pty_session._WindowsPty.__new__(_pty_session._WindowsPty)
    p._closed = False
    p._io_lock = threading.Lock()
    p._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    p._proc = MagicMock()
    return p


def test_pty_eviction_unregisters(monkeypatch, clean_serve_ptys, fake_posix_kill):
    # _evict_old_ptys reads PTYS_MAX from its defining module (server.pty),
    # so the cap override must be patched there, not on the serve re-export.
    monkeypatch.setattr(_server_pty, "PTYS_MAX", 2)
    oldest = _add_lifecycle_entry("oldest", "ended", "2026-05-26T00:00:00+00:00")
    _add_lifecycle_entry("newer", "ended", "2026-05-26T00:00:01+00:00")
    _add_lifecycle_entry("newest", "ended", "2026-05-26T00:00:02+00:00")

    serve._evict_old_ptys()

    with serve.PTYS_LOCK:
        pty_count = len(serve.PTYS)
        remaining = set(serve.PTYS)
    assert oldest._closed is True
    assert remaining == {"newer", "newest"}
    assert _pty_session.Pty.active_count() == pty_count


def test_pty_kill_unregisters(clean_serve_ptys, fake_posix_kill):
    pty = _add_lifecycle_entry("kill-me", "running", "2026-05-26T00:00:00+00:00")
    before = _pty_session.Pty.active_count()

    assert serve._pty_kill("kill-me") is True

    with serve.PTYS_LOCK:
        assert "kill-me" not in serve.PTYS
        pty_count = len(serve.PTYS)
    assert pty._closed is True
    assert _pty_session.Pty.active_count() == before - 1
    assert _pty_session.Pty.active_count() == pty_count == 0


def test_pty_churn_30_does_not_exhaust(monkeypatch, clean_serve_ptys, fake_posix_kill):
    monkeypatch.setattr(_server_pty, "PTYS_MAX", 20)
    for i in range(30):
        pty_id = f"pty-{i}"
        _add_lifecycle_entry(pty_id, "running", f"2026-05-26T00:00:{i:02d}+00:00")
        assert serve._pty_kill(pty_id) is True

    with serve.PTYS_LOCK:
        assert len(serve.PTYS) == 0
    assert _pty_session.Pty.active_count() == 0


def test_cleanup_idle_timer_wired():
    # The idle thread is wired up in serve.py's main(); the loop body that
    # actually calls cleanup_idle now lives in server/pty.py (re-exported as
    # serve._pty_idle_loop), so check that via getsource — same approach as
    # the storage/agent_runs extractions.
    assert re.search(
        r"threading\.Thread\([^)]*target=\s*_pty_idle_loop",
        _SERVE_SOURCE,
    )
    assert re.search(
        r"_pty_session\.Pty\.cleanup_idle\(\)",
        inspect.getsource(serve._pty_idle_loop),
    )


def test_shutdown_handlers_registered():
    assert re.search(
        r"atexit\.register\(_shutdown_all_ptys\)",
        _SERVE_SOURCE,
    )
    assert re.search(
        r"signal\.signal\(signal\.SIGTERM",
        _SERVE_SOURCE,
    )


def test_windows_init_lifecycle_propagates_exception(monkeypatch):
    """If ``PtyProcess.spawn`` raises mid-construction, ``_WindowsPty``
    must propagate the exception and the caller must NOT receive a
    half-constructed instance. Since Python destroys the instance when
    __init__ raises, the registry is also safe (Pty.spawn's _register
    runs AFTER __init__ returns)."""
    # We can't easily monkeypatch the conditional `from winpty import
    # PtyProcess` inside __init__, so we install a fake `winpty` module
    # into sys.modules and let the import inside __init__ pick it up.
    fake_module = type(sys)("winpty")
    fake_module.__version__ = "2.0.0"

    class _BadPtyProcess:
        @staticmethod
        def spawn(*args, **kwargs):
            raise RuntimeError("conpty init exploded")

    fake_module.PtyProcess = _BadPtyProcess
    monkeypatch.setitem(sys.modules, "winpty", fake_module)

    with pytest.raises(RuntimeError, match="conpty init exploded"):
        _pty_session._WindowsPty(
            ["pwsh.exe"], cwd=None, env=None, cols=80, rows=24,
        )

    # Active count must remain 0 — the failed __init__ never reached
    # Pty.spawn's _register call.
    assert _pty_session.Pty.active_count() == 0


def test_windows_init_lifecycle_cleans_up_partial_proc(monkeypatch, caplog):
    """If ``PtyProcess.spawn`` succeeds but a downstream construction
    step (decoder) raises, the spawn's child process must be terminated
    before the exception propagates."""
    fake_module = type(sys)("winpty")
    fake_module.__version__ = "2.0.0"

    fake_proc = MagicMock()

    class _OkPtyProcess:
        @staticmethod
        def spawn(*args, **kwargs):
            return fake_proc

    fake_module.PtyProcess = _OkPtyProcess
    monkeypatch.setitem(sys.modules, "winpty", fake_module)

    # Force the decoder construction to blow up after spawn succeeds.
    real_decoder = codecs.getincrementaldecoder
    def _boom(name):  # noqa: ARG001
        raise ValueError("decoder boom")
    monkeypatch.setattr(codecs, "getincrementaldecoder", _boom)

    with caplog.at_level(logging.WARNING, logger="pty_session"):
        with pytest.raises(ValueError, match="decoder boom"):
            _pty_session._WindowsPty(
                ["pwsh.exe"], cwd=None, env=None, cols=80, rows=24,
            )

    # The stranded proc MUST have been terminate()d.
    fake_proc.terminate.assert_called_once()
    # Restore decoder for any later tests in the same process.
    monkeypatch.setattr(codecs, "getincrementaldecoder", real_decoder)


def test_cleanup_idle_logs_on_kill_exception(caplog):
    """When a victim's kill() raises, cleanup_idle must log a warning
    rather than swallow silently — and still continue with _unregister
    so the registry doesn't grow."""
    # Construct a fake pty.
    fake = _fake_winpty()

    # Patch kill to raise; verify cleanup_idle logs + unregisters anyway.
    with patch.object(
        _pty_session._WindowsPty, "kill",
        side_effect=RuntimeError("kill boom"),
    ):
        # Register manually (we bypassed Pty.spawn). _register stamps
        # _last_io with the current monotonic clock — override it AFTER
        # register so cleanup_idle's "idle for >timeout" check fires.
        _pty_session.Pty._register(fake)
        fake._last_io = 0.0  # epoch — guaranteed idle vs any future time
        try:
            with caplog.at_level(logging.WARNING, logger="pty_session"):
                victims = _pty_session.Pty.cleanup_idle(timeout=1.0)
        finally:
            # Make sure no stale state survives across tests.
            _pty_session.Pty._unregister(fake)

    assert fake in victims, (
        "cleanup_idle must still return the victim even if kill raised"
    )
    matching = [
        r for r in caplog.records
        if "cleanup_idle" in r.getMessage() and r.levelno == logging.WARNING
    ]
    assert matching, (
        f"Expected a cleanup_idle warning record, got: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    # Registry must be empty (idempotent _unregister handled).
    assert id(fake) not in _pty_session.Pty._registry


def test_windows_resize_log_includes_traceback(caplog):
    """Functional check: a resize() exception must produce a log record
    whose ``exc_info`` is populated (i.e. ``exc_info=True`` was passed)."""
    p = _fake_winpty()
    p._proc.setwinsize.side_effect = RuntimeError("setwinsize boom")

    with caplog.at_level(logging.WARNING, logger="pty_session"):
        p.resize(cols=100, rows=40)  # must not raise

    matching = [r for r in caplog.records if "resize" in r.getMessage().lower()]
    assert matching, "Expected at least one resize-related warning record"
    rec = matching[0]
    # exc_info=True populates rec.exc_info as a 3-tuple of (type, exc, tb).
    assert rec.exc_info is not None and rec.exc_info[0] is RuntimeError, (
        "resize warning must capture exc_info (exc_info=True missing)"
    )
