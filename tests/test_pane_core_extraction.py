"""Characterization tests for the PaneCore extraction (plan Task 6).

These assert the structural invariants that the chat-pane render/stream
move out of ``terminals.js`` into ``app/pane-core.js`` must satisfy. They
mirror the plain-pathlib, source-asserting style of
``tests/test_dashboard_sanitization.py`` (no node required).

Marker substring choice: the chat composer's placeholder string

    "type, /skill, @file, paste/drop images, Enter sends · Shift+Enter newline"

is a stable, unique fragment of the chat-pane DOM template that
``termOpen`` used to build inline. After the extraction it must live in
``pane-core.js`` (the new owner of the chat-pane template) and must be
absent from ``terminals.js`` (which now delegates to ``PaneCore.mount``).
It was chosen because it is verbatim, appears exactly once in the chat
template, and is not a layout affordance (so it genuinely tracks where
the chat template lives, not incidental markup).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANE_CORE = ROOT / ".ai/dashboard/app/pane-core.js"
TERMINALS = ROOT / ".ai/dashboard/app/terminals.js"
INDEX_HTML = ROOT / ".ai/dashboard/index.html"

# Distinctive, verbatim fragment of the chat composer placeholder in the
# chat-pane template (documented above).
CHAT_TEMPLATE_MARKER = "type, /skill, @file, paste/drop images, Enter sends"


def test_pane_core_file_exists():
    assert PANE_CORE.exists(), "app/pane-core.js must exist"


def test_pane_core_defines_mount_and_fetch_meta():
    src = PANE_CORE.read_text(encoding="utf-8")
    assert "mount" in src, "PaneCore must define mount"
    assert "fetchMeta" in src, "PaneCore must define fetchMeta"


def test_pane_core_ends_with_window_export():
    src = PANE_CORE.read_text(encoding="utf-8")
    # Single global export line near the end (no ES-module export).
    assert "window.PaneCore =" in src, "pane-core.js must export window.PaneCore"
    # The export object must surface the contract surface.
    assert "mount" in src and "fetchMeta" in src


def test_pane_core_is_not_an_es_module():
    src = PANE_CORE.read_text(encoding="utf-8")
    # No ES-module keywords — every dashboard script is a plain global script.
    for kw in ("\nexport ", "\nimport ", "export default", "export {"):
        assert kw not in src, f"pane-core.js must not use ES-module syntax ({kw!r})"


def test_index_loads_pane_core_before_terminals():
    html = INDEX_HTML.read_text(encoding="utf-8")
    pc = html.find('app/pane-core.js')
    term = html.find('app/terminals.js')
    assert pc != -1, "index.html must load app/pane-core.js"
    assert term != -1, "index.html must load app/terminals.js"
    assert pc < term, "pane-core.js must be loaded BEFORE terminals.js"


def test_chat_template_moved_out_of_terminals():
    src = TERMINALS.read_text(encoding="utf-8")
    assert CHAT_TEMPLATE_MARKER not in src, (
        "chat-pane template marker must no longer live in terminals.js — "
        "it moved into pane-core.js"
    )


def test_chat_template_present_in_pane_core():
    src = PANE_CORE.read_text(encoding="utf-8")
    assert CHAT_TEMPLATE_MARKER in src, (
        "chat-pane template marker must now live in pane-core.js"
    )


def test_terminals_delegates_to_pane_core_mount():
    src = TERMINALS.read_text(encoding="utf-8")
    assert "PaneCore.mount(" in src, (
        "terminals.js termOpen shim must call PaneCore.mount"
    )
