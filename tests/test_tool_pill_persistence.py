"""Regression: streamed assistant tool pills must PERSIST in the chat.

The bug: assistant markdown is repainted by overwriting the ``.text`` element's
innerHTML on every streaming frame. Tool pills / thinking blocks / todos are
appended as children of that SAME ``.text`` — so rendering markdown straight
into ``.text`` wiped them on the next repaint ("the tool shows while running,
then disappears and never stays in the chain").

The fix renders streamed text into a dedicated open ``.md`` *segment* child and
appends non-text children (pills/thinking/todo) as siblings, CLOSING the segment
so the next text opens a fresh one below the pill. Both the canvas renderer
(pane-core.js) and the dashboard renderer (terminals.js) carry the fix.

These are source-level assertions (no browser): they pin the structural
invariant — text repaints target a SEGMENT, never the shared ``.text`` — in both
files so the regression can't silently return.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANE_CORE = ROOT / ".ai/dashboard/app/pane-core.js"
TERMINALS = ROOT / ".ai/dashboard/app/terminals.js"


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    """Return the source between the line containing start_marker and the next
    occurrence of end_marker (so we scope assertions to one function body)."""
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    j = src.find(end_marker, i + len(start_marker))
    assert j != -1, f"end marker not found after {start_marker!r}: {end_marker!r}"
    return src[i:j]


# ─── pane-core.js (canvas renderer) ──────────────────────────────────────────


def test_pane_core_defines_segment_helpers():
    src = PANE_CORE.read_text(encoding="utf-8")
    assert "function paneCoreOpenTextSegment(" in src
    assert "function paneCoreCloseTextSegment(" in src


def test_pane_core_append_repaints_segment_not_shared_text():
    """paneCoreAppendAssistantText must repaint the SEGMENT's innerHTML, never
    the shared .text innerHTML (which would nuke sibling pills)."""
    src = PANE_CORE.read_text(encoding="utf-8")
    body = _slice(src, "function paneCoreAppendAssistantText(", "\nfunction ")
    assert "paneCoreOpenTextSegment(" in body, "append must open a text segment"
    assert "seg.innerHTML" in body, "append must write the segment's innerHTML"
    assert "textEl.innerHTML" not in body, (
        "append must NOT overwrite the shared .text innerHTML (wipes pills)"
    )


def test_pane_core_tool_pill_closes_segment():
    """Adding a tool pill must close the open segment so subsequent text lands
    below the pill instead of overwriting it."""
    src = PANE_CORE.read_text(encoding="utf-8")
    body = _slice(src, "function paneCoreAddToolPill(", "\nfunction ")
    assert "paneCoreCloseTextSegment(" in body


# ─── terminals.js (dashboard renderer) ───────────────────────────────────────


def test_terminals_defines_segment_helpers():
    src = TERMINALS.read_text(encoding="utf-8")
    assert "function termOpenTextSegment(" in src
    assert "function termCloseTextSegment(" in src


def test_terminals_append_repaints_segment_not_shared_text():
    src = TERMINALS.read_text(encoding="utf-8")
    body = _slice(src, "function termAppendAssistantText(", "\n    function ")
    assert "termOpenTextSegment(" in body, "append must open a text segment"
    assert "seg.innerHTML" in body, "append must write the segment's innerHTML"
    assert "textEl.innerHTML" not in body, (
        "append must NOT overwrite the shared .text innerHTML (wipes pills)"
    )


def test_terminals_tool_pill_closes_segment():
    src = TERMINALS.read_text(encoding="utf-8")
    body = _slice(src, "function termAddToolPill(", "\n    // ----- Inline tool-detail")
    assert "termCloseTextSegment(" in body
