"""Source-level guards for the perf-focused terminals.js refactor.

These tests assert the SHAPE of the code (array buffer pattern, diff cliff
guard, debounce wiring, normalize gating) rather than runtime behaviour —
the dashboard runs in a browser and has no node test harness here. The
patterns checked correspond 1:1 to the four perf fixes documented in the
executor packet.
"""

import re
from pathlib import Path

TERMINALS_JS = (
    Path(__file__).resolve().parent.parent
    / ".ai"
    / "dashboard"
    / "app"
    / "terminals.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _region(src: str, anchor: str, span: int = 1200) -> str:
    """Return ``span`` characters starting at the first match of ``anchor``.

    Lets us assert patterns appear *inside* a specific function without
    being fooled by an identical pattern in some unrelated handler.
    """
    idx = src.find(anchor)
    assert idx != -1, f"anchor {anchor!r} not found in terminals.js"
    return src[idx : idx + span]


def test_codex_chunk_buffer_uses_array() -> None:
    """termHandleCodexChunk must push deltas into an array, not string-concat."""
    src = _src()
    region = _region(src, "function termHandleCodexChunk")
    assert "jsonBuf.push(" in region, (
        "expected array buffer pattern (jsonBuf.push) in termHandleCodexChunk"
    )
    # The old O(n²) concat must be gone from this handler.
    assert 't.jsonBuf = (t.jsonBuf || "") + chunk' not in region, (
        "termHandleCodexChunk still uses the old string-concat pattern"
    )


def test_chat_chunk_buffer_uses_array() -> None:
    """termHandleChatChunk must push deltas into an array, not string-concat."""
    src = _src()
    region = _region(src, "function termHandleChatChunk")
    assert "jsonBuf.push(" in region, (
        "expected array buffer pattern (jsonBuf.push) in termHandleChatChunk"
    )
    # The old `t.jsonBuf += chunk` must be gone from this handler.
    assert "t.jsonBuf += chunk" not in region, (
        "termHandleChatChunk still uses the old string-concat pattern"
    )


def test_simple_line_diff_has_size_guard() -> None:
    """simpleLineDiff must short-circuit huge diffs before allocating the DP grid."""
    src = _src()
    region = _region(src, "function simpleLineDiff")
    # Must reference the size product somewhere in the guard.
    has_explicit = bool(
        re.search(r"oldLines\.length\s*\*\s*newLines\.length", region)
        or re.search(r"\bn\s*\*\s*m\b", region)
    )
    assert has_explicit, "simpleLineDiff size guard expression not found"
    # And the guard must lead to an early return (fallback) before the DP loop.
    guard_match = re.search(
        r"(oldLines\.length\s*\*\s*newLines\.length|\bn\s*\*\s*m\b)[^\n]*\n[^\n]*return",
        region,
    )
    assert guard_match, "size guard does not lead to an early return"


def test_termRunSearch_is_debounced() -> None:
    """The search input handler must coalesce keystrokes via setTimeout/clearTimeout."""
    src = _src()
    # Find the input listener wiring region. The anchor is the input registration.
    anchor_idx = src.find('searchInput.addEventListener("input"')
    assert anchor_idx != -1, "search input listener not found"
    region = src[anchor_idx : anchor_idx + 400]
    assert "setTimeout" in region, "search input listener is missing setTimeout (no debounce)"
    assert "clearTimeout" in region, "search input listener is missing clearTimeout (no debounce reset)"


def test_body_normalize_gated() -> None:
    """t.body.normalize() must live inside a conditional, not as a bare call.

    We verify there is an `if (...) {` opening brace immediately preceding
    each ``t.body.normalize()`` call, with at most a single statement (the
    normalize line itself or a guard flag read) between the brace and the
    call.  An ungated bare call would have a function-scope ``{`` or a
    semicolon-terminated statement directly above it instead.
    """
    src = _src()
    found = False
    for match in re.finditer(r"t\.body\.normalize\(\)", src):
        found = True
        start = match.start()
        prefix = src[max(0, start - 300) : start]
        # The closest enclosing brace must come from an `if (...) {` line.
        # Find the last `{` and check the line above it contains `if (`.
        last_brace_idx = prefix.rfind("{")
        assert last_brace_idx != -1, "no enclosing brace found above normalize()"
        # Look at text from the start of the line containing that brace
        # backwards a bit, and confirm an `if (` token sits on the same line.
        line_start = prefix.rfind("\n", 0, last_brace_idx) + 1
        opening_line = prefix[line_start : last_brace_idx + 1]
        assert "if (" in opening_line, (
            "t.body.normalize() is not directly inside an `if (...) {` block; "
            f"opening line was: {opening_line.strip()!r}"
        )
    assert found, "no t.body.normalize() call found in terminals.js"
