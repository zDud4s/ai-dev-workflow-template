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

import os
import shutil
import sys


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
    on Windows and getting PowerShell)."""
    if not name or name == "auto":
        return detect_default_shell()
    name = name.lower().strip()
    aliases = {
        "bash":       ["bash", "-l"],
        "zsh":        ["zsh", "-l"],
        "sh":         ["sh"],
        "fish":       ["fish", "-l"],
        "cmd":        ["cmd.exe"],
        "powershell": ["powershell.exe", "-NoLogo"],
        "pwsh":       ["pwsh", "-NoLogo"],
    }
    argv = aliases.get(name)
    if not argv:
        raise FileNotFoundError(f"unknown shell {name!r}")
    bin_path = shutil.which(argv[0])
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


class Pty:
    """Platform-agnostic PTY handle.

    Construct via :func:`spawn`; the concrete class is selected once at
    spawn time so the rest of the codebase only deals with the common
    methods (``read`` / ``write`` / ``resize`` / ``kill`` / ``alive``).
    """

    def read(self, n: int = 4096) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    def alive(self) -> bool:
        raise NotImplementedError

    @property
    def pid(self) -> int:
        raise NotImplementedError

    @staticmethod
    def spawn(
        argv: list[str],
        cwd: str | None = None,
        env: dict | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> "Pty":
        if sys.platform == "win32":
            return _WindowsPty(argv, cwd=cwd, env=env, cols=cols, rows=rows)
        return _PosixPty(argv, cwd=cwd, env=env, cols=cols, rows=rows)


# ---------- POSIX ----------

class _PosixPty(Pty):
    def __init__(self, argv, cwd, env, cols, rows):
        import pty as _pty
        import fcntl as _fcntl
        import termios as _termios
        import struct as _struct

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
            except Exception:
                pass
            os._exit(127)
        self._pid = pid
        self._fd = fd
        self._closed = False
        # Apply requested winsize before any output streams.
        try:
            ws = _struct.pack("HHHH", rows, cols, 0, 0)
            _fcntl.ioctl(fd, _termios.TIOCSWINSZ, ws)
        except OSError:
            pass

    def read(self, n=4096) -> bytes:
        if self._closed:
            return b""
        try:
            data = os.read(self._fd, n)
        except OSError:
            return b""
        if not data:
            # EOF on the master: child closed its end.
            self._closed = True
        return data

    def write(self, data: bytes) -> int:
        if self._closed:
            return 0
        try:
            return os.write(self._fd, data)
        except OSError:
            self._closed = True
            return 0

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        import fcntl as _fcntl
        import termios as _termios
        import struct as _struct
        try:
            ws = _struct.pack("HHHH", rows, cols, 0, 0)
            _fcntl.ioctl(self._fd, _termios.TIOCSWINSZ, ws)
        except OSError:
            pass

    def kill(self) -> None:
        import signal as _signal
        if not self._closed:
            try:
                os.kill(self._pid, _signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._closed = True

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

        # pywinpty 2.x API: PtyProcess.spawn returns a PtyProcess instance
        # backed by ConPTY. dimensions are (rows, cols).
        self._proc = PtyProcess.spawn(
            argv,
            cwd=cwd,
            env=child_env,
            dimensions=(rows, cols),
        )
        self._closed = False

    def read(self, n=4096) -> bytes:
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
        if isinstance(chunk, str):
            return chunk.encode("utf-8", errors="replace")
        return bytes(chunk)

    def write(self, data: bytes) -> int:
        if self._closed:
            return 0
        try:
            text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else data
            self._proc.write(text)
            return len(data)
        except OSError:
            self._closed = True
            return 0

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def kill(self) -> None:
        if self._closed:
            return
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass
        self._closed = True

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
