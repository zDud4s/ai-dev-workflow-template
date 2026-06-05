"""Static-lint tests for the SessionPane frontend (Task 9).

Asserts that terminals.js contains the two new top-level functions
(termOpenSession / termSendSession) and that they reference the correct
unified API endpoints and session-state vocabulary introduced in Tasks 5-8.
No JS runtime is available; all checks are source-level string assertions,
matching the established pattern in tests/test_terminals_medium.py.
"""

from pathlib import Path

TERMINALS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"


def js():
    return TERMINALS_JS.read_text(encoding="utf-8")


def test_has_session_pane_open_and_send():
    src = js()
    assert "function termOpenSession" in src
    assert "function termSendSession" in src


def test_session_pane_uses_unified_endpoints():
    src = js()
    assert "/api/sessions/" in src
    assert "/stream" in src
    assert "/input" in src


def test_session_pane_consumes_state_change_and_states():
    src = js()
    assert "state_change" in src            # consumes the SessionEvent state frames
    assert "acquiring" in src               # new state strings the chip switches on
    assert "engine" in src


def test_session_pane_send_is_wired():
    src = js()
    # composer always-on path: send goes through termSendSession, not the old fork gate
    assert "termSendSession" in src
    assert "termOpenSession" in src
