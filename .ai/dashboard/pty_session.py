"""Cross-platform PTY (pseudo-terminal) helper for the dashboard.

The dashboard's "Terminals" page can host real shell sessions (cmd /
powershell / bash / zsh / ...) running inside the project directory.
Each session lives in a true PTY so curses/TUI apps (claude, codex,
vim, htop, less, fzf) work correctly — unlike the existing chat-claude
/ chat-codex panes which pipe stream-json over plain stdin/stdout.

POSIX path uses stdlib (``pty.fork`` + ``os.read``/``write``).
Windows path uses ``pywinpty`` (ConPTY binding). pywinpty is an
optional runtime dependency: when missing, ``Pty.spawn`` raises a
clear ImportError so the HTTP layer can return 503.

Usage:
    p = Pty.spawn(["bash"], cwd="/path/to/project", cols=120, rows=30)
    p.write(b"echo hi\\r")
    data = p.read(4096)        # blocks until bytes are ready
    p.resize(cols=140, rows=40)
    p.kill()

Reader-thread orchestration (broadcasting bytes to one or more WS
subscribers, ring-buffering for late attach) lives in serve.py — this
file is the platform abstraction only.
"""
from __future__ import annotations

import codecs
import errno
import logging
import os
import shutil
import struct
import sys
import threading
import time as _time_mod
from abc import ABC, abstractmethod


# DoS guardrails. The HTTP layer maps Pty.spawn exceptions to 503, so
# raising RuntimeError once the cap is hit is enough to reject new
# sessions. Actual idle-timeout firing lives in serve.py's reader loop,
# which can call ``Pty.cleanup_idle()`` periodically — this module just
# exposes the registry/API.
MAX_PTY_SESSIONS = 32
PTY_IDLE_TIMEOUT_S = 1800

# Reap-loop timing constants. Named so the meaning is obvious at the
# call site (a bare ``0.5`` / ``0.05`` was previously sprinkled across
# ``kill()`` / ``read()`` / ``_reap_with_timeout`` with no comment).
# - ``REAP_TIMEOUT_S``: hard budget per SIGTERM (and per SIGKILL) before
#   we stop waiting for the child to clear from the process table.
# - ``REAP_POLL_INTERVAL_S``: sleep between non-blocking waitpid checks.
# - ``EOF_REAP_TIMEOUT_S``: shorter budget for the EOF-triggered reap on
#   POSIX read() — the child has already closed its PTY end, so it's
#   effectively gone; don't block the reader on a full half-second.
REAP_TIMEOUT_S = 0.5
REAP_POLL_INTERVAL_S = 0.05
EOF_REAP_TIMEOUT_S = 0.05

# Module-level logger. The caller is responsible for wiring handlers
# (serve.py / dashboard process); we install a NullHandler so that
# ``pty_session`` never crashes or emits "no handler found" warnings
# when used as a library by a host that hasn't configured logging.
_log = logging.getLogger("pty_session")
_log.addHandler(logging.NullHandler())

# Known-good absolute paths for the allowlisted shells. We probe these
# BEFORE falling back to ``shutil.which`` so a hostile/early PATH entry
# (e.g. a planted ``bash.exe``) can't hijack the spawn. PATH lookup is
# still used as a fallback to keep cross-platform / nix-store / brew
# layouts working.
_TRUSTED_SHELL_PATHS: dict[str, tuple[str, ...]] = {
    "bash":       ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"),
    "zsh":        ("/bin/zsh", "/usr/bin/zsh", "/usr/local/bin/zsh"),
    "sh":         ("/bin/sh", "/usr/bin/sh"),
    "fish":       ("/usr/bin/fish", "/usr/local/bin/fish", "/opt/homebrew/bin/fish"),
    "cmd":        (r"C:\Windows\System32\cmd.exe",),
    "powershell": (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",),
    "pwsh":       (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Program Files (x86)\PowerShell\7\pwsh.exe",
    ),
}

# Trusted directory prefixes for the ``shutil.which`` fallback path.
# Even when ``_TRUSTED_SHELL_PATHS`` misses (nix-store, brew, custom
# install layouts), we require ``shutil.which`` to resolve to a binary
# whose absolute path starts with one of these prefixes — otherwise an
# attacker who plants a rogue ``bash`` in e.g. ``%TEMP%`` and prepends
# it to PATH could still hijack the spawn. Keep this list conservative:
# system bin dirs + Nix/Homebrew/Scoop/Chocolatey/standard Windows
# install roots. Adding a new layout here is a deliberate trust
# decision, not an accident.
_TRUSTED_SHELL_DIRS: tuple[str, ...] = (
    # POSIX system bins
    "/bin/",
    "/sbin/",
    "/usr/bin/",
    "/usr/sbin/",
    "/usr/local/bin/",
    "/usr/local/sbin/",
    # macOS Homebrew (Apple Silicon + Intel)
    "/opt/homebrew/bin/",
    "/opt/homebrew/sbin/",
    "/usr/local/Cellar/",
    # Nix
    "/nix/store/",
    "/run/current-system/sw/bin/",
    # Windows system
    r"c:\windows\system32\\",
    r"c:\windows\syswow64\\",
    # Windows PowerShell 7 / pwsh
    r"c:\program files\powershell\\",
    r"c:\program files (x86)\powershell\\",
    # Windows Git Bash / MSYS2 / Cygwin (canonical install roots)
    r"c:\program files\git\\",
    r"c:\program files (x86)\git\\",
    r"c:\msys64\\",
    r"c:\cygwin64\\",
    # Windows Scoop / Chocolatey (user-scoped but well-known)
    r"c:\programdata\chocolatey\\",
)


def _is_under_trusted_dir(path: str) -> bool:
    """Return True iff ``path`` lives under one of ``_TRUSTED_SHELL_DIRS``.

    Case-insensitive on Windows; uses ``os.path.normpath`` to collapse
    ``..`` segments and normalize separators so an attacker can't escape
    the check with e.g. ``C:\\Windows\\System32\\..\\..\\evil.exe``.
    """
    if not path:
        return False
    try:
        norm = os.path.normpath(path)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        norm_cmp = norm.lower().replace("/", "\\")
        if not norm_cmp.endswith("\\"):
            norm_cmp_dir = norm_cmp + "\\"
        else:
            norm_cmp_dir = norm_cmp
        for prefix in _TRUSTED_SHELL_DIRS:
            pfx = prefix.lower().replace("/", "\\").rstrip("\\") + "\\"
            if norm_cmp.startswith(pfx) or norm_cmp_dir.startswith(pfx):
                return True
        return False
    # POSIX: case-sensitive, forward slashes.
    norm_cmp = norm
    if not norm_cmp.endswith("/"):
        norm_cmp_dir = norm_cmp + "/"
    else:
        norm_cmp_dir = norm_cmp
    for prefix in _TRUSTED_SHELL_DIRS:
        if "\\" in prefix:
            continue  # Windows-only prefix on POSIX host
        pfx = prefix.rstrip("/") + "/"
        if norm_cmp.startswith(pfx) or norm_cmp_dir.startswith(pfx):
            return True
    return False


def detect_default_shell() -> list[str]:
    """Return the argv for a sensible default shell on this platform.

    Windows: prefer ``pwsh`` (Powershell 7+) if on PATH, else
    ``powershell`` (Windows PowerShell 5.1), else ``cmd.exe``.
    macOS / Linux: honor ``$SHELL`` if set; otherwise zsh, bash, sh.
    """
    if sys.platform == "win32":
        for cand in ("pwsh.exe", "pwsh", "powershell.exe", "powershell"):
            p = shutil.which(cand)
            if p:
                return [p]
        return [os.environ.get("COMSPEC", "cmd.exe")]
    shell = os.environ.get("SHELL")
    if shell and os.path.exists(shell):
        return [shell, "-l"]
    for cand in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if os.path.exists(cand):
            return [cand, "-l"]
    return ["/bin/sh"]


def resolve_shell(name: str | None) -> list[str]:
    """Map a user-facing shell name ("auto" / "bash" / "pwsh" / ...) to
    a concrete argv.

    ``auto`` (or empty) picks the platform default. An explicit shell
    name that isn't on PATH raises FileNotFoundError so the HTTP layer
    can return 503 with a clear message — silently substituting the
    default shell would surprise the operator (e.g. asking for ``zsh``
    on Windows and getting PowerShell).

    Security note (PATH-injection hardening): the alias whitelist below
    restricts user input to seven well-known shell names. For each, we
    probe ``_TRUSTED_SHELL_PATHS`` (canonical absolute paths) BEFORE
    ``shutil.which``, so a hostile/early ``PATH`` entry that drops a
    rogue ``bash.exe`` can't hijack the spawn unless the trusted path
    is also absent. ``shutil.which`` remains the fallback for non-
    standard layouts — but the resolved path is further gated against
    ``_TRUSTED_SHELL_DIRS`` so an attacker who plants a binary in e.g.
    ``%TEMP%`` and prepends it to PATH still can't hijack the spawn.
    Callers MUST NOT pass arbitrary user input as ``name`` — the alias
    dict is the trust boundary.
    """
    if not name or name == "auto":
        return detect_default_shell()
    name = name.strip().lower()
    aliases = {
        "bash":       ("bash", "-l"),
        "zsh":        ("zsh", "-l"),
        "sh":         ("sh",),
        "fish":       ("fish", "-l"),
        "cmd":        ("cmd.exe",),
        "powershell": ("powershell.exe", "-NoLogo"),
        "pwsh":       ("pwsh", "-NoLogo"),
    }
    template = aliases.get(name)
    if template is None:
        raise FileNotFoundError(f"unknown shell {name!r}")
    # Build a FRESH list each call so mutating the returned argv (or
    # caching it) can't corrupt the next caller's result.
    argv = list(template)
    # Prefer trusted absolute paths over PATH lookup to harden against
    # PATH injection — a planted ``bash.exe`` earlier on PATH would
    # otherwise be selected silently.
    bin_path: str | None = None
    for cand in _TRUSTED_SHELL_PATHS.get(name, ()):
        if os.path.exists(cand):
            bin_path = cand
            break
    if not bin_path:
        candidate = shutil.which(argv[0])
        # Trusted-dir gate: reject anything that doesn't live under a
        # well-known system / package-manager bin directory. Closes the
        # residual PATH-injection risk without breaking nix / brew /
        # scoop / system-level installs. If a user has a custom shell
        # install layout, they must add the prefix to _TRUSTED_SHELL_DIRS
        # explicitly (deliberate trust decision).
        if candidate and _is_under_trusted_dir(candidate):
            bin_path = candidate
        elif candidate:
            _log.warning(
                "resolve_shell: rejecting untrusted PATH resolution for %r: %r "
                "(not under _TRUSTED_SHELL_DIRS)",
                name,
                candidate,
            )
    if not bin_path:
        raise FileNotFoundError(f"shell {name!r} not on PATH")
    argv[0] = bin_path
    return argv


def is_shell_available(name: str | None) -> bool:
    """Cheap probe: returns True iff ``resolve_shell`` would succeed."""
    if not name or name == "auto":
        return True
    try:
        resolve_shell(name)
        return True
    except FileNotFoundError:
        return False


class Pty(ABC):
    """Platform-agnostic PTY handle.

    Construct via :func:`spawn`; the concrete class is selected once at
    spawn time so the rest of the codebase only deals with the common
    methods (``read`` / ``write`` / ``resize`` / ``kill`` / ``alive``).

    The ``pid`` property returns an ``int`` on every platform. POSIX
    implementations always return a positive process id (the forked
    child). The Windows implementation returns ``-1`` as a sentinel
    when the underlying ConPTY process is not yet spawned or has
    already been killed — callers comparing ``pty.pid > 0`` get a
    well-defined "no live pid" signal instead of relying on Optional.
    """

    # Module-level registry of live PTYs, used to enforce
    # ``MAX_PTY_SESSIONS`` and to expose idle-timeout cleanup. The lock
    # guards both ``_registry`` and per-entry ``last_io`` updates.
    _registry: "dict[int, Pty]" = {}
    _registry_lock = threading.Lock()

    @abstractmethod
    def read(self, n: int = 4096) -> bytes:
        """Read up to ``n`` bytes from the PTY master.

        Returns ``b""`` on EOF (child closed its end) or on a transient
        EAGAIN/EWOULDBLOCK condition — callers must distinguish via
        ``alive()`` if needed. Blocks on platforms with blocking I/O.
        """
        raise NotImplementedError

    @abstractmethod
    def write(self, data: bytes) -> int:
        """Write ``data`` to the PTY master.

        Returns the number of bytes accepted. Implementations loop on
        short writes where the OS supports it (POSIX); zero indicates
        the PTY is closed.
        """
        raise NotImplementedError

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY window. Best-effort — silent on transient
        OS failures (e.g. ConPTY rejecting resize during teardown)."""
        raise NotImplementedError

    @abstractmethod
    def kill(self) -> None:
        """Terminate the child process and release OS resources.

        Idempotent by design: a second call must NOT touch already-closed
        fds/handles (which would mask genuine bugs behind EBADF noise).
        """
        raise NotImplementedError

    @abstractmethod
    def alive(self) -> bool:
        """Return True iff the child process is still running.

        May proactively reap zombies (POSIX) or flip ``_closed`` to True
        as a side effect when it detects the child has exited.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def pid(self) -> int:
        """Child process id, or ``-1`` on Windows when no live process.

        POSIX always returns the positive fork-child pid; Windows returns
        the ConPTY pid when alive and ``-1`` once killed or pre-spawn.
        """
        raise NotImplementedError

    # ----- registry / idle tracking -----

    @classmethod
    def _register(cls, pty: "Pty") -> None:
        with cls._registry_lock:
            cls._registry[id(pty)] = pty
            pty._last_io = _time_mod.monotonic()

    @classmethod
    def _unregister(cls, pty: "Pty") -> None:
        with cls._registry_lock:
            cls._registry.pop(id(pty), None)

    @staticmethod
    def _touch(pty: "Pty") -> None:
        # Cheap stamp update — no lock; a slightly stale timestamp is
        # acceptable since cleanup_idle re-reads under the lock.
        try:
            pty._last_io = _time_mod.monotonic()
        except Exception:
            pass

    @classmethod
    def active_count(cls) -> int:
        with cls._registry_lock:
            return len(cls._registry)

    @classmethod
    def cleanup_idle(cls, timeout: float = PTY_IDLE_TIMEOUT_S) -> list["Pty"]:
        """Kill PTYs that haven't seen I/O for ``timeout`` seconds.

        Returns the list of PTYs that were killed. The serve.py reader
        loop is expected to call this periodically — wiring the timer
        lives there, not in this module.
        """
        now = _time_mod.monotonic()
        victims: list[Pty] = []
        with cls._registry_lock:
            for pty in list(cls._registry.values()):
                last = getattr(pty, "_last_io", now)
                if now - last >= timeout:
                    victims.append(pty)
        for pty in victims:
            try:
                pty.kill()
            except Exception:
                # Best-effort: a kill failure here shouldn't block cleanup
                # of the other victims. Log with traceback so operators can
                # spot a recurring failure pattern (e.g. PtyProcess in a
                # bad state) instead of silently dropping the diagnostic.
                _log.warning(
                    "cleanup_idle: kill() failed for pty id=%s",
                    id(pty),
                    exc_info=True,
                )
            cls._unregister(pty)
        return victims

    @classmethod
    def spawn(
        cls,
        argv: list[str],
        cwd: str | None = None,
        env: dict | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> "Pty":
        # Hard cap to prevent fd exhaustion from runaway dashboard JS or
        # an adversary spamming spawn. serve.py maps this RuntimeError to
        # HTTP 503.
        if cls.active_count() >= MAX_PTY_SESSIONS:
            raise RuntimeError("max PTY sessions reached")
        _log.info("PTY spawn: argv=%s cwd=%s", argv, cwd)
        if sys.platform == "win32":
            pty = _WindowsPty(argv, cwd=cwd, env=env, cols=cols, rows=rows)
        else:
            pty = _PosixPty(argv, cwd=cwd, env=env, cols=cols, rows=rows)
        cls._register(pty)
        return pty


# ---------- POSIX ----------

class _PosixPty(Pty):
    def __init__(self, argv, cwd, env, cols, rows):
        # ``fcntl`` and ``termios`` are POSIX-only; importing them at the
        # module top would break the import on Windows hosts that still
        # need to load this file so they reach ``_WindowsPty``. ``pty``
        # is also POSIX-only. ``struct`` is already available at module
        # top (used in the exception handler below), so no local alias
        # needed — the previous ``import struct as _struct`` was a dead
        # alias of an already-imported name.
        import pty as _pty
        import fcntl as _fcntl
        import termios as _termios

        self._argv = list(argv)
        pid, fd = _pty.fork()
        if pid == 0:
            # In the child process: set up cwd/env, then exec the shell.
            # Anything that goes wrong here must os._exit() — raising
            # would corrupt the parent's run-state via fork-after-exec.
            try:
                if cwd:
                    try:
                        os.chdir(cwd)
                    except OSError:
                        pass
                child_env = dict(env) if env else os.environ.copy()
                # Reasonable defaults so ncurses apps render correctly.
                child_env.setdefault("TERM", "xterm-256color")
                child_env.setdefault("COLORTERM", "truecolor")
                # Strip any incoming Claude / Codex pipe-mode env vars so
                # an interactive `claude` / `codex` started inside the
                # spawned shell behaves normally.
                for var in ("CLAUDE_CODE_ACTION", "CLAUDE_CODE_STREAM_JSON"):
                    child_env.pop(var, None)
                os.execvpe(argv[0], argv, child_env)
            except Exception as exc:
                # Surface the failure to the PTY master (fd 2 is the
                # child's stderr, which the parent reads from the master
                # side and forwards to the WebSocket). Without this the
                # operator only sees "terminal closed immediately" with
                # no clue why ``exec`` failed.
                try:
                    _log.warning("PTY exec failed: %s", exc)
                except Exception:
                    pass
                try:
                    msg = f"pty: exec failed: {exc}\n".encode(
                        "utf-8", errors="replace"
                    )
                    os.write(2, msg)
                except Exception:
                    pass
            os._exit(127)
        self._pid = pid
        self._fd = fd
        self._closed = False
        # Apply requested winsize before any output streams.
        try:
            ws = struct.pack("HHHH", rows, cols, 0, 0)
            _fcntl.ioctl(fd, _termios.TIOCSWINSZ, ws)
        except (OSError, struct.error):
            pass

    def read(self, n=4096) -> bytes:
        if self._closed:
            return b""
        try:
            data = os.read(self._fd, n)
        except OSError as e:
            # Distinguish "no data right now" (EAGAIN/EWOULDBLOCK) from
            # legitimate EOF signals so async/non-blocking callers don't
            # see a spurious close. EIO is Linux's signal that the slave
            # side closed; EBADF means our fd is gone. Anything else is
            # unexpected — return empty bytes WITHOUT flipping ``_closed``
            # so the caller can retry rather than silently losing the fd.
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                _log.debug("PTY read EAGAIN")
                return b""
            if e.errno in (errno.EIO, errno.EBADF):
                self._closed = True
                return b""
            # Unexpected errno — log so operators have a paper trail
            # instead of silently dropping bytes. Don't flip _closed:
            # caller can retry via alive() / next read().
            _log.warning(
                "PTY read unexpected OSError errno=%s: %s",
                e.errno,
                e,
            )
            return b""
        Pty._touch(self)
        if not data:
            # EOF on the master: child closed its end. Proactively reap
            # so we don't leave a zombie sitting around waiting for the
            # next alive() call. Use ``EOF_REAP_TIMEOUT_S`` (shorter than
            # the full ``REAP_TIMEOUT_S``) — the child has already closed
            # its PTY end, so it's effectively gone.
            self._closed = True
            try:
                self._reap_with_timeout(EOF_REAP_TIMEOUT_S)
            except Exception:
                pass
        return data

    def write(self, data: bytes) -> int:
        if self._closed:
            return 0
        # POSIX ``os.write`` may return fewer bytes than requested when
        # the kernel buffer fills up. Loop until everything is flushed or
        # the fd reports EOF (write returning 0) — otherwise keystrokes
        # can silently disappear into the void.
        total = 0
        view = memoryview(data)
        while total < len(view):
            try:
                n = os.write(self._fd, view[total:])
            except BlockingIOError:
                # fd is blocking by default; surfacing this means caller
                # set O_NONBLOCK. Yield back what we managed to write.
                return total
            except OSError:
                self._closed = True
                return total
            if n == 0:
                # Treat as EOF — further writes would also be 0.
                self._closed = True
                return total
            total += n
        Pty._touch(self)
        return total

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        # POSIX-only stdlib pair; see ``_PosixPty.__init__`` for why these
        # are imported inside the method rather than at module top.
        # ``struct`` is already module-level (used in the except clause
        # below); the previous ``import struct as _struct`` local was a
        # dead alias.
        import fcntl as _fcntl
        import termios as _termios
        try:
            ws = struct.pack("HHHH", rows, cols, 0, 0)
            _fcntl.ioctl(self._fd, _termios.TIOCSWINSZ, ws)
        except (OSError, struct.error):
            pass

    def kill(self) -> None:
        # ``signal`` is the only stdlib module specific to this method;
        # imported here (not at module top) because the entire POSIX
        # branch — including kill() — must stay loadable on Windows hosts
        # where stdlib ``signal`` lacks SIGTERM/SIGKILL constants used by
        # the ``os.kill`` calls below. A previous version also imported
        # ``time`` here, but it was never used inside this method (only
        # in ``_reap_with_timeout``) — dead branch, removed.
        import signal as _signal

        # Idempotent by design: a second kill() call must NOT try to
        # close an already-closed fd (which would yield EBADF and be
        # silently eaten by the os.close try/except, masking real bugs).
        if self._closed:
            return
        try:
            os.kill(self._pid, _signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        if not self._reap_with_timeout(REAP_TIMEOUT_S):
            _log.warning(
                "PTY kill: SIGKILL escalation for pid=%d", self._pid
            )
            try:
                os.kill(self._pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self._reap_with_timeout(REAP_TIMEOUT_S)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._closed = True
        _log.info("PTY closed: pid=%d", self._pid)
        Pty._unregister(self)

    def _reap_with_timeout(self, timeout: float) -> bool:
        # ``time`` is already imported at module top as ``_time_mod`` for
        # the registry/idle-tracking helpers; reuse that handle instead
        # of a redundant per-call import so a tracing tool / mock can
        # patch a single name.
        deadline = _time_mod.monotonic() + timeout
        while _time_mod.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                return True
            except OSError:
                return False
            if pid != 0:
                return True
            _time_mod.sleep(REAP_POLL_INTERVAL_S)
        return False

    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            pid, _status = os.waitpid(self._pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        if pid == 0:
            return True
        # Child reaped; PTY master will hit EOF on next read.
        self._closed = True
        return False

    @property
    def pid(self) -> int:
        return self._pid


# ---------- Windows ----------

class _WindowsPty(Pty):
    def __init__(self, argv, cwd, env, cols, rows):
        try:
            import winpty as _winpty_mod
            from winpty import PtyProcess
        except ImportError as e:
            raise ImportError(
                "pywinpty is required for terminal sessions on Windows. "
                "Install with: pip install pywinpty"
            ) from e

        child_env = dict(env) if env else dict(os.environ)
        child_env.setdefault("TERM", "xterm-256color")
        child_env.setdefault("COLORTERM", "truecolor")
        for var in ("CLAUDE_CODE_ACTION", "CLAUDE_CODE_STREAM_JSON"):
            child_env.pop(var, None)

        # Serializes kill() against any in-flight read() touching
        # ``self._proc``. pywinpty's thread-safety is not formally
        # documented; ``terminate(force=True)`` triggers Win32
        # ``TerminateProcess`` asynchronously, so if a reader thread is
        # mid-``ReadFile`` we want kill() to wait for that syscall to
        # return (terminate causes the next read to surface EOFError
        # quickly). The lock makes the race tractable without papering
        # over it with sleeps.
        self._io_lock = threading.Lock()

        # pywinpty env-format compatibility. pywinpty 1.x expects a
        # list of ``"K=V"`` strings; pywinpty 2.x accepts a dict. Detect
        # by inspecting the module's ``__version__`` (best-effort — if
        # the attribute is missing or malformed we assume modern 2.x).
        pywinpty_version = getattr(_winpty_mod, "__version__", "") or ""
        if isinstance(pywinpty_version, str) and pywinpty_version.startswith("1."):
            # pywinpty 1.x: list-of-KV-strings
            env_for_spawn = [f"{k}={v}" for k, v in child_env.items()]
        else:
            # pywinpty 2.x+: dict
            env_for_spawn = child_env

        # pywinpty 2.x API: PtyProcess.spawn returns a PtyProcess instance
        # backed by ConPTY. dimensions are (rows, cols).
        #
        # Lifecycle guard: if ``PtyProcess.spawn`` raises (bad argv, ConPTY
        # init failure, missing entry-point exe, etc.) we must NOT leave a
        # half-constructed instance — ``Pty.spawn`` would otherwise pass it
        # to ``_register`` and the registry would gain a corpse with no
        # ``_proc`` attribute, breaking ``read()``/``kill()`` later. Same
        # rationale if a downstream assignment (e.g. building the decoder)
        # somehow raised: tear down ``_proc`` first, then re-raise.
        self._proc = None  # type: ignore[assignment]
        self._closed = True  # default-closed until init succeeds
        try:
            self._proc = PtyProcess.spawn(
                argv,
                cwd=cwd,
                env=env_for_spawn,
                dimensions=(rows, cols),
            )
            self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            self._closed = False
        except Exception as exc:
            _log.warning(
                "PTY Windows spawn failed: %s", exc, exc_info=True
            )
            # If pywinpty handed us a proc before raising downstream, kill
            # it so we don't leak a ConPTY child. terminate() is best-effort.
            proc = self._proc
            self._proc = None
            if proc is not None:
                try:
                    proc.terminate(force=True)
                except Exception:
                    _log.debug(
                        "PTY Windows spawn cleanup: terminate failed",
                        exc_info=True,
                    )
            raise
        _log.info("PTY Windows session spawned: argv=%s cwd=%s", argv, cwd)

    def read(self, n=4096) -> bytes:
        if self._closed:
            return b""
        # Hold _io_lock for the duration of the underlying pywinpty
        # ReadFile so a concurrent kill() can't free/terminate _proc
        # mid-syscall. terminate() blocks on this lock; once we return
        # (or raise EOFError after terminate fires), kill() proceeds.
        with self._io_lock:
            if self._closed:
                return b""
            try:
                chunk = self._proc.read(n)
            except EOFError:
                self._closed = True
                return b""
            except OSError:
                self._closed = True
                return b""
        if chunk is None:
            return b""
        Pty._touch(self)
        if isinstance(chunk, bytes):
            # Already bytes — return directly to avoid a redundant copy
            # via ``bytes(chunk)`` (no-op wrapper that still allocates).
            return chunk
        if isinstance(chunk, bytearray):
            # bytearray → bytes requires a real copy; do it once here.
            return bytes(chunk)
        if isinstance(chunk, str):
            return chunk.encode("utf-8", errors="replace")
        # Defensive fallback for unexpected types from pywinpty.
        return bytes(chunk)

    def write(self, data: bytes) -> int:
        if self._closed:
            return 0
        _log.debug("PTY Windows write %d bytes", len(data))
        try:
            if isinstance(data, (bytes, bytearray)):
                text = self._decoder.decode(bytes(data), final=False)
            else:
                text = data
            self._proc.write(text)
            Pty._touch(self)
            return len(data)
        except OSError:
            self._closed = True
            return 0

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        try:
            self._proc.setwinsize(rows, cols)
        except Exception as exc:
            # Best-effort: ConPTY occasionally rejects resize during
            # process teardown. Log a diagnostic so operators can spot a
            # genuine failure pattern (vs. the single benign rejection
            # at shutdown) without crashing the session. ``exc_info=True``
            # so the traceback is captured at DEBUG-level handlers.
            _log.warning(
                "PTY Windows resize failed: %s", exc, exc_info=True
            )

    def kill(self) -> None:
        # Idempotent: skip if already closed so a double-call doesn't
        # touch the underlying proc twice.
        if self._closed:
            return
        # Serialize against any reader thread mid-read. ``terminate``
        # is async (TerminateProcess), so a concurrent ReadFile on
        # ``self._proc`` would race; holding the lock ensures the
        # reader returns (typically as EOFError, since terminate
        # closes the ConPTY handle) before we mark ourselves closed.
        with self._io_lock:
            if self._closed:
                return
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass
            self._closed = True
            try:
                pid = int(self._proc.pid)
            except Exception:
                pid = -1
        _log.info("PTY closed: pid=%d", pid)
        Pty._unregister(self)

    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            ok = bool(self._proc.isalive())
        except Exception:
            ok = False
        if not ok:
            self._closed = True
        return ok

    @property
    def pid(self) -> int:
        try:
            return int(self._proc.pid)
        except Exception:
            return -1
