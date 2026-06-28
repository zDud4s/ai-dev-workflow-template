import codecs
import os
import pathlib
import signal
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / ".ai" / "dashboard"))
from server import pty_session as _pty_session


def _fake_posix_pty():
    """Create a _PosixPty instance with fake state; bypasses real fork."""
    p = _pty_session._PosixPty.__new__(_pty_session._PosixPty)
    p._closed = False
    p._pid = 99001
    p._fd = -1
    return p


def test_kill_reaps_child():
    p = _fake_posix_pty()
    wnohang = 1
    waitpid_calls = []

    def fake_waitpid(pid, flags):
        waitpid_calls.append((pid, flags))
        return (p._pid, 0)

    with patch.object(os, "WNOHANG", wnohang, create=True), \
         patch.object(os, "kill") as mock_kill, \
         patch.object(os, "waitpid", side_effect=fake_waitpid), \
         patch.object(os, "close"):
        p.kill()

    sent_signals = [call.args[1] for call in mock_kill.call_args_list]
    assert signal.SIGTERM in sent_signals, "kill() did not send SIGTERM"
    assert any(flags == wnohang for _, flags in waitpid_calls), \
        "kill() did not reap with waitpid(WNOHANG)"
    assert p._closed is True


def test_kill_escalates_to_sigkill():
    p = _fake_posix_pty()
    wnohang = 1
    sigkill = 9
    sent_signals = []

    def fake_kill(pid, sig):
        sent_signals.append(sig)

    def fake_waitpid(pid, flags):
        if sigkill in sent_signals:
            return (pid, 0)
        return (0, 0)

    with patch.object(os, "WNOHANG", wnohang, create=True), \
         patch.object(signal, "SIGKILL", sigkill, create=True), \
         patch.object(os, "kill", side_effect=fake_kill), \
         patch.object(os, "waitpid", side_effect=fake_waitpid), \
         patch.object(os, "close"), \
         patch("time.sleep"):
        p.kill()

    assert signal.SIGTERM in sent_signals, "SIGTERM not sent"
    assert sigkill in sent_signals, "SIGKILL escalation missing"
    assert sent_signals.index(signal.SIGTERM) < sent_signals.index(sigkill), \
        "SIGKILL sent before SIGTERM"
    assert p._closed is True


def test_resize_swallows_struct_error():
    fake_fcntl = MagicMock()
    fake_termios = MagicMock()
    fake_termios.TIOCSWINSZ = 0

    with patch.dict(sys.modules, {"fcntl": fake_fcntl, "termios": fake_termios}):
        p = _fake_posix_pty()
        p.resize(cols=99999, rows=99999)
        fake_fcntl.ioctl.assert_not_called()

        p.resize(cols=80, rows=24)
        fake_fcntl.ioctl.assert_called_once()


def test_incremental_utf8_decoder_split():
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    output = decoder.decode(b"\xc3", final=False) + decoder.decode(b"\xa9", final=False)

    assert output == "é"
    assert "�" not in output
