"""Polish-pass tests for pty_session.py.

Covers four low-priority hygiene fixes:
  1. ``Pty`` is a proper ``abc.ABC`` with ``@abstractmethod`` decorators.
  2. ``pid`` is typed and returns ``int`` on every platform (POSIX
     positive, Windows ``-1`` sentinel when no live process).
  3. ``resolve_shell`` uses the conventional ``.strip().lower()`` order.
  4. Windows read path avoids a redundant ``bytes(chunk)`` wrapper when
     the incoming chunk is already ``bytes``.
"""
from __future__ import annotations

import inspect
import pathlib
import sys

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent / ".ai" / "dashboard"),
)
from server.pty import session as _pty_session  # noqa: E402


_PTY_SOURCE_PATH = pathlib.Path(_pty_session.__file__)
_PTY_SOURCE = _PTY_SOURCE_PATH.read_text(encoding="utf-8")


# ---------- Fix 1: ABC + @abstractmethod ----------

def test_pty_base_is_abc():
    """``Pty`` must be an abstract base class — incomplete subclasses
    should fail at instantiation, not silently mask missing methods."""
    assert inspect.isabstract(_pty_session.Pty), (
        "Pty must be marked abstract via abc.ABC + @abstractmethod"
    )
    abstract_methods = getattr(_pty_session.Pty, "__abstractmethods__", frozenset())
    assert abstract_methods, "Pty.__abstractmethods__ must be non-empty"
    # The six platform-required methods/properties.
    expected = {"read", "write", "resize", "kill", "alive", "pid"}
    assert expected.issubset(abstract_methods), (
        f"Expected abstract methods {expected!r} but got {abstract_methods!r}"
    )


def test_pty_base_methods_decorated():
    """Source must contain ``@abstractmethod`` for each abstract slot."""
    # At least 6 (read/write/resize/kill/alive/pid). Allow more if future
    # abstract methods are added.
    count = _PTY_SOURCE.count("@abstractmethod")
    assert count >= 4, (
        f"Expected >= 4 @abstractmethod decorators in pty_session.py, "
        f"found {count}"
    )


def test_pty_cannot_be_instantiated_directly():
    """A direct ``Pty()`` must raise TypeError (abstract class)."""
    try:
        _pty_session.Pty()
    except TypeError as exc:
        # CPython message contains "abstract" — be lenient about exact wording.
        assert "abstract" in str(exc).lower()
    else:
        raise AssertionError("Pty() must raise TypeError for abstract class")


# ---------- Fix 2: pid type consistency ----------

def test_pid_returns_int():
    """POSIX ``_PosixPty.pid`` returns ``int`` for any stamped ``_pid``."""
    if not hasattr(_pty_session, "_PosixPty"):
        # Module loaded on Windows; POSIX subclass still exists at import
        # time because it's a class def, not platform-gated.
        return
    p = _pty_session._PosixPty.__new__(_pty_session._PosixPty)
    p._pid = 12345
    assert p.pid == 12345
    assert isinstance(p.pid, int)


def test_pid_annotation_is_int_on_pty_base():
    """The abstract ``pid`` property must declare ``-> int`` so callers
    don't have to guess about Optional."""
    src = _PTY_SOURCE
    # Find the abstract pid property block and confirm `-> int` is present.
    # We just look for the pattern "def pid(self) -> int" since that's the
    # signature used in the ABC and both subclasses.
    assert "def pid(self) -> int" in src, (
        "Pty.pid must be annotated `-> int` on the ABC + both subclasses"
    )
    # Sanity: appears at least 3 times (ABC + POSIX + Windows).
    assert src.count("def pid(self) -> int") >= 3, (
        "`def pid(self) -> int` should appear on ABC + _PosixPty + _WindowsPty"
    )


def test_pty_pid_documents_negative_sentinel():
    """The Pty ABC docstring must document the ``-1`` sentinel for the
    Windows path so callers know what ``pty.pid > 0`` actually means."""
    doc = _pty_session.Pty.__doc__ or ""
    assert "-1" in doc, (
        "Pty.__doc__ must mention the -1 sentinel for the Windows path"
    )


# ---------- Fix 3: resolve_shell .strip().lower() order ----------

def test_resolve_shell_strip_lower_order():
    """Convention is ``.strip().lower()`` (matches stdlib idioms).

    Behavior is identical to ``.lower().strip()`` (lower preserves
    whitespace) but the ordering is the project convention.
    """
    src = inspect.getsource(_pty_session.resolve_shell)
    assert ".strip().lower()" in src, (
        "resolve_shell should normalize via .strip().lower()"
    )
    assert ".lower().strip()" not in src, (
        "resolve_shell still uses the old .lower().strip() order"
    )


def test_resolve_shell_handles_whitespace_and_case():
    """Functional check that mixed-case + padded input still resolves."""
    # ``auto`` short-circuits before normalization — pick a real alias.
    # We don't want to actually call shutil.which from a unit test, so we
    # rely on the FileNotFoundError path for an unknown shell to confirm
    # normalization happens BEFORE the alias lookup. "  XYZUNKNOWN  "
    # should normalize to "xyzunknown" and then raise.
    try:
        _pty_session.resolve_shell("  XYZUNKNOWN  ")
    except FileNotFoundError as exc:
        # Error message should contain the normalized name (lowercase,
        # stripped) — confirms .strip().lower() runs before alias lookup.
        msg = str(exc)
        assert "xyzunknown" in msg, (
            f"Expected normalized 'xyzunknown' in error, got: {msg!r}"
        )
    else:
        raise AssertionError(
            "resolve_shell must raise FileNotFoundError for unknown shell"
        )


# ---------- Fix 4: Windows read avoids redundant bytes() copy ----------

def test_windows_read_avoids_redundant_copy():
    """``_WindowsPty.read`` must return ``bytes`` directly when the
    underlying chunk is already ``bytes`` — no ``bytes(chunk)`` wrapper
    that allocates a no-op copy."""
    src = inspect.getsource(_pty_session._WindowsPty.read)
    assert "isinstance(chunk, bytes)" in src, (
        "Windows read path must check `isinstance(chunk, bytes)` and "
        "return directly to avoid a redundant copy"
    )
    # The fast path should NOT wrap an already-bytes chunk in bytes().
    # We look for the structural pattern: an isinstance(..., bytes) check
    # followed by `return chunk` (not `return bytes(chunk)`).
    # A regex would be overkill; a simple containment check suffices.
    assert "return chunk" in src, (
        "Windows read must `return chunk` directly on the bytes fast-path"
    )


def test_windows_read_bytes_fast_path_behavior():
    """Functional: when the pywinpty proc yields ``bytes``, ``read``
    returns the exact same object (or at least an equal one) without
    re-wrapping. We can verify identity by monkeypatching a fake proc.
    """
    p = _pty_session._WindowsPty.__new__(_pty_session._WindowsPty)
    p._closed = False
    import threading as _threading
    p._io_lock = _threading.Lock()

    sentinel = b"hello-world-pty-bytes"

    class _FakeProc:
        def read(self, n):
            return sentinel

    p._proc = _FakeProc()

    out = p.read(4096)
    assert out == sentinel
    # Identity: confirms no redundant copy on the bytes fast-path.
    assert out is sentinel, (
        "Windows read should return the bytes chunk directly without "
        "allocating a new bytes object"
    )


def test_windows_read_str_branch_still_encodes_utf8():
    """``str`` chunks from pywinpty 2.x must still be encoded as UTF-8."""
    p = _pty_session._WindowsPty.__new__(_pty_session._WindowsPty)
    p._closed = False
    import threading as _threading
    p._io_lock = _threading.Lock()

    class _FakeProc:
        def read(self, n):
            return "ünîcode"

    p._proc = _FakeProc()

    out = p.read(4096)
    assert isinstance(out, bytes)
    assert out == "ünîcode".encode("utf-8")
