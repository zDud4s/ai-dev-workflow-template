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
PANE_CORE_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "pane-core.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _pane_core_src() -> str:
    return PANE_CORE_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function whose signature matches ``header``.

    Naive brace-matcher — adequate for this codebase where function bodies are
    well-formed and balanced.
    """
    idx = src.find(header)
    assert idx != -1, f"could not locate {header!r}"
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
    src = _pane_core_src()
    # The fallback path must stash the listener on the term object so a
    # later close path can hand it to removeEventListener.
    mount_body = _slice_function(src, "function paneCoreMountPty(")
    assert "t._resizeFallback = debouncedResize" in mount_body, (
        "ResizeObserver-absent fallback must capture its listener in "
        "t._resizeFallback so paneCoreMountPty's close path can remove it"
    )
    close_idx = mount_body.find("const closePty = () =>")
    assert close_idx != -1, "paneCoreMountPty must define a local PTY close path"
    close_body = mount_body[close_idx:]
    assert 'removeEventListener("resize"' in close_body, (
        "paneCoreMountPty's close path must removeEventListener('resize', ...) for the "
        "fallback listener — otherwise it leaks per closed PTY"
    )
    assert "_resizeFallback" in close_body, (
        "paneCoreMountPty's close path must consult t._resizeFallback when removing the "
        "fallback resize listener"
    )


def test_dispatch_tracker_registry_removed_from_terminals_surface():
    """CRITICAL 2: dispatch tracker registry must not retain detached panes."""
    src = _src()
    assert "DISPATCH_TRACKERS" not in src, (
        "DISPATCH_TRACKERS must stay removed; without the registry there are "
        "no stale tracker entries for termClose to clean up"
    )
    assert "function termOpenDispatchTracker" not in src, (
        "dispatch tracker panes must not be opened inline from terminals.js"
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
    src = _pane_core_src()
    chat_body = _slice_function(src, "function paneCoreMountChat(")
    # The chat-pane keydown handler lives just below its send button click
    # listener in the isolated canvas renderer.
    anchor = 'sendBtn.addEventListener("click", () => paneCoreSend(t));'
    idx = chat_body.find(anchor)
    assert idx != -1, "chat-pane send-button anchor not found"
    window = chat_body[idx : idx + 600]
    # Must call termSend on Enter AND must include an isComposing check.
    assert "paneCoreSend(t)" in window, "chat-pane Enter must still call paneCoreSend"
    enter_block = re.search(r'if\s*\(\s*e\.key\s*===\s*"Enter".*?paneCoreSend\(t\)', window, re.DOTALL)
    assert enter_block is not None, "Enter→paneCoreSend(t) block not located after anchor"
    assert "isComposing" in enter_block.group(0), (
        "chat-pane Enter handler must check !e.isComposing to avoid sending "
        "mid-IME-composition input"
    )


# Removed: the dead-chat resume path (termSendResumeChat) was deleted when
# Claude chats converged on the unified session pane.
