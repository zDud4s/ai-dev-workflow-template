"""Canvas-convergence source checks for terminals.js."""

from __future__ import annotations

import re
from pathlib import Path


TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
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


def test_removed_non_pty_inline_builders_are_absent():
    src = _src()
    assert re.search(r"\bfunction\s+termOpenSession\s*\(", src) is None
    assert re.search(r"\bfunction\s+termOpen\s*\(", src) is None
    assert re.search(r"\bfunction\s+termOpenDispatchTracker\s*\(", src) is None


def test_start_conversation_claude_posts_input_then_routes_canvas():
    body = _slice_function(_src(), "const startConversation = async ()")
    assert 'window.open("app/canvas.html", "dash-canvas")' in body
    assert '"/api/sessions/" + encodeURIComponent(sid) + "/input"' in body
    assert "owner: termClientId()" in body
    assert "payload.model = model" in body
    assert 'termSendToCanvas(_statusRowTerm("session", "session:" + sid))' in body
    assert "termOpenSession(" not in body
    assert "termSendSession(" not in body


def test_start_conversation_codex_routes_job_to_canvas():
    body = _slice_function(_src(), "const startConversation = async ()")
    assert 'postJson("/api/jobs", payload)' in body
    assert '{ kind: "chat-codex", task: text, model }' in body
    assert 'termSendToCanvas(_statusRowTerm("chat-codex", res.id))' in body
    assert "termOpen(res.id" not in body


def test_legacy_v1_persistence_removed():
    src = _src()
    assert "migrateOpenPanesV1ToV2" not in src
    assert "dash.openPanes.v1" not in src
