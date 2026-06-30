"""Regression: live chain-of-thought must stream into the chat, like tool pills.

The bug: the live stream-json renderer surfaced ``tool_use`` blocks (via
``content_block_start``) and assistant text (via ``text_delta``) in real time,
but SILENTLY DROPPED thinking — both the ``content_block_start`` of type
``thinking`` and the ``thinking_delta`` events that carry the streaming thought
text. So during a turn the operator saw tools appear live but never the model's
reasoning ("só aparece as tools"). Thinking only showed up batched at turn-end
via the final ``assistant`` frame.

Verified against a real ``claude --output-format stream-json
--include-partial-messages`` capture: thinking arrives as
``stream_event.event.delta.type == "thinking_delta"`` (text at
``event.delta.thinking``) preceded by a ``content_block_start`` whose
``content_block.type == "thinking"``.

The fix routes those events into a live ``.thinking-block`` (expanded while
streaming, collapsed on ``content_block_stop``) and dedupes the final assistant
frame against it so the block isn't rendered twice. Both renderers carry it:
terminals.js (dashboard) and pane-core.js (canvas).

Source-level assertions (no browser), mirroring test_tool_pill_persistence.py,
pin the invariant in BOTH files so the regression can't silently return.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANE_CORE = ROOT / ".ai/dashboard/app/pane-core.js"
TERMINALS = ROOT / ".ai/dashboard/app/terminals.js"


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    """Return source between start_marker and the next end_marker (scopes
    assertions to one function/handler body)."""
    i = src.find(start_marker)
    assert i != -1, f"marker not found: {start_marker!r}"
    j = src.find(end_marker, i + len(start_marker))
    assert j != -1, f"end marker not found after {start_marker!r}: {end_marker!r}"
    return src[i:j]


# ─── terminals.js (dashboard renderer) ───────────────────────────────────────


def test_terminals_defines_thinking_delta_helper():
    src = TERMINALS.read_text(encoding="utf-8")
    assert "function termAppendThinkingDelta(" in src, (
        "terminals.js must define a live thinking-delta renderer"
    )
    assert "function termFinishThinking(" in src, (
        "terminals.js must define a finalizer that collapses the live block"
    )


def test_terminals_stream_event_routes_thinking_delta():
    """The live stream_event handler must route thinking_delta (and the thinking
    content_block_start) into the live renderer — not drop them."""
    src = TERMINALS.read_text(encoding="utf-8")
    body = _slice(src, 'if (type === "stream_event")', "// Genuinely unknown")
    assert "thinking_delta" in body, "stream_event must handle thinking_delta"
    assert "termAppendThinkingDelta(" in body, (
        "stream_event must feed thinking deltas to the live renderer"
    )
    assert "content_block_stop" in body, (
        "stream_event must finalize the live thinking block on content_block_stop"
    )


def test_terminals_final_assistant_frame_dedupes_thinking():
    """When thinking already streamed live, the final assistant frame must NOT
    render the thinking block again (avoid the duplicate-block syndrome)."""
    src = TERMINALS.read_text(encoding="utf-8")
    assert "thinkingLive" in src, (
        "terminals.js must mark live-streamed thinking so the final frame dedupes"
    )


# ─── pane-core.js (canvas renderer) ──────────────────────────────────────────


def test_pane_core_defines_thinking_delta_helper():
    src = PANE_CORE.read_text(encoding="utf-8")
    assert "function paneCoreAppendThinkingDelta(" in src
    assert "function paneCoreFinishThinking(" in src


def test_pane_core_stream_event_routes_thinking_delta():
    src = PANE_CORE.read_text(encoding="utf-8")
    body = _slice(src, 'if (type === "stream_event")', "const pre = document.createElement")
    assert "thinking_delta" in body, "stream_event must handle thinking_delta"
    assert "paneCoreAppendThinkingDelta(" in body
    assert "content_block_stop" in body


def test_pane_core_final_assistant_frame_dedupes_thinking():
    src = PANE_CORE.read_text(encoding="utf-8")
    assert "thinkingLive" in src
