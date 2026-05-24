"""Static-lint tests for the 2026-05-22 terminals.js bug-hunt fixes.

The dashboard has no jsdom harness, so we assert on source-level invariants
that prove the fixes are present. Pattern modeled on
``tests/test_jobs_static_refactor.py``.
"""

import re
from pathlib import Path

TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function whose signature matches ``header``.

    Naive brace-matcher — adequate for this codebase where function bodies are
    well-formed and balanced.
    """
    idx = src.find(header)
    assert idx != -1, f"could not locate {header!r} in terminals.js"
    brace = src.find("{", idx)
    assert brace != -1
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
    raise AssertionError(f"unterminated function body for {header!r}")


def test_resize_fallback_listener_tracked():
    """CRITICAL 1: fallback resize listener must be capturable + removable."""
    src = _src()
    # The fallback path must stash the listener on the term object so a
    # later close path can hand it to removeEventListener.
    assert "_resizeFallback" in src, (
        "ResizeObserver-absent fallback must capture its listener in "
        "t._resizeFallback so termClosePty can remove it"
    )
    pty_body = _slice_function(src, "function termClosePty(")
    assert 'removeEventListener("resize"' in pty_body, (
        "termClosePty must removeEventListener('resize', ...) for the "
        "fallback listener — otherwise it leaks per closed PTY"
    )
    assert "_resizeFallback" in pty_body, (
        "termClosePty must consult t._resizeFallback when removing the "
        "fallback resize listener"
    )


def test_dispatch_trackers_cleanup_in_close():
    """CRITICAL 2: termClose must purge DISPATCH_TRACKERS so stale entries
    don't keep detached panes alive."""
    src = _src()
    close_body = _slice_function(src, "function termClose(")
    assert "DISPATCH_TRACKERS" in close_body, (
        "termClose must clean up DISPATCH_TRACKERS — termCloseAllFinished "
        "and persistence-driven closes bypass the in-pane close-button "
        "listener that previously owned this cleanup"
    )
    assert "DISPATCH_TRACKERS.delete" in close_body, (
        "termClose must actually .delete the tracker entry"
    )


def test_self_test_console_logs_removed():
    """MEDIUM 3: the simpleLineDiff self-test must no longer log on every page
    load. Either fully removed, or gated behind a DEBUG_ flag."""
    src = _src()
    # The literal "OK" log was the production-noise smoking gun.
    assert 'console.log("[dashboard] simpleLineDiff self-test' not in src, (
        "the unconditional simpleLineDiff self-test console.log must be "
        "removed or gated behind DEBUG_DIFF_SELFTEST"
    )
    # The "FAILED" / "threw" error logs should also be gone or gated. If
    # they remain, they must be inside a DEBUG_ guard.
    if "simpleLineDiff self-test FAILED" in src or "simpleLineDiff threw" in src:
        assert "DEBUG_DIFF_SELFTEST" in src or "DEBUG_" in src, (
            "self-test error logs must be gated behind a DEBUG_ flag"
        )


def test_chat_enter_checks_ime_composition():
    """HIGH 4: the chat-pane composer Enter handler must not fire mid-IME
    composition (Japanese/Chinese/Korean candidate selection)."""
    src = _src()
    # The chat-pane keydown handler lives just below `sendBtn.addEventListener
    # ("click", () => termSend(t));`. Locate that anchor and inspect the
    # following keydown block.
    anchor = 'sendBtn.addEventListener("click", () => termSend(t));'
    idx = src.find(anchor)
    assert idx != -1, "chat-pane send-button anchor not found"
    window = src[idx : idx + 600]
    # Must call termSend on Enter AND must include an isComposing check.
    assert "termSend(t)" in window, "chat-pane Enter must still call termSend"
    enter_block = re.search(r'if\s*\(\s*e\.key\s*===\s*"Enter".*?termSend\(t\)', window, re.DOTALL)
    assert enter_block is not None, "Enter→termSend(t) block not located after anchor"
    assert "isComposing" in enter_block.group(0), (
        "chat-pane Enter handler must check !e.isComposing to avoid sending "
        "mid-IME-composition input"
    )


def test_resume_chat_disabled_before_guard():
    """HIGH 5: termSendResumeChat must disable the send button BEFORE the
    empty-text early return, so a synchronous re-entry sees the disabled
    button instead of double-posting."""
    src = _src()
    body = _slice_function(src, "async function termSendResumeChat(")
    disable_idx = body.find("t.sendBtn.disabled = true")
    return_idx = body.find("if (!trimmed) return")
    assert disable_idx != -1, "termSendResumeChat must disable the send button"
    assert return_idx != -1, "termSendResumeChat must still guard against empty input"
    assert disable_idx < return_idx, (
        "t.sendBtn.disabled = true must appear BEFORE the trimmed-empty "
        "early return so a fast double-click can't slip a second send in "
        "between the trim check and the disable"
    )
