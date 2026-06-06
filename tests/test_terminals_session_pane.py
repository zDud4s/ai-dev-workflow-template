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


def test_session_pane_closes_stream_on_collapse():
    src = js()
    # session panes must participate in the lazy-stream collapse lifecycle
    # (close EventSource on collapse) like transcript panes, to avoid leaking
    # one of the browser's ~6 HTTP/1.1 connections per collapsed pane.
    assert "closeStream" in src
    # termSetCollapsed must handle the session kind, not only transcript
    import re as _re
    # crude but effective: the collapse handler references "session" alongside the stream toggle
    assert _re.search(r'kind\s*===\s*"session"', src), "termSetCollapsed should handle kind === 'session'"


# ------------------------------------------------------------------
# Task A: foreign chip, queued suffix, warning rendering
# ------------------------------------------------------------------

def test_chip_foreign_state_and_external_label():
    src = js()
    # termSessionChipUpdate must have a branch for the "foreign" state
    assert '"foreign"' in src, 'termSessionChipUpdate should branch on state === "foreign"'
    assert '"external"' in src, 'the foreign-state pill label should be "external"'


def test_chip_queued_suffix():
    src = js()
    # When t.pending is true the chip label gets a " · queued" suffix
    assert "t.pending" in src, "termSessionChipUpdate should read t.pending"
    assert '"queued"' in src or "queued" in src, 'a "queued" label/suffix must appear'


def test_handle_session_event_stores_pending():
    src = js()
    # state_change handler must also store t.pending = !!ev.pending
    assert "t.pending" in src and "ev.pending" in src, (
        "termHandleSessionEvent should store t.pending = !!ev.pending on state_change"
    )


def test_handle_session_event_warning_kind():
    src = js()
    # termHandleSessionEvent must handle kind === "warning" frames
    assert '"warning"' in src, 'termHandleSessionEvent should branch on kind === "warning"'


# ------------------------------------------------------------------
# Task B: queue-aware send — explicit 202 and 409 branches
# ------------------------------------------------------------------

def test_send_session_inspects_status_202():
    src = js()
    assert "202" in src, "termSendSession should handle HTTP 202 (queued) explicitly"


def test_send_session_inspects_status_409():
    src = js()
    assert "409" in src, "termSendSession should handle HTTP 409 (already_queued) explicitly"


# ------------------------------------------------------------------
# Task C: release + interrupt controls in pane header
# ------------------------------------------------------------------

def test_open_session_has_release_control():
    src = js()
    assert '"/release"' in src or "'/release'" in src or "/release" in src, (
        "termOpenSession should wire a release fetch to /api/sessions/<sid>/release"
    )


def test_open_session_has_interrupt_control():
    src = js()
    assert '"/interrupt"' in src or "'/interrupt'" in src or "/interrupt" in src, (
        "termOpenSession should wire an interrupt fetch to /api/sessions/<sid>/interrupt"
    )
