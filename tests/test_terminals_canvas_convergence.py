"""Canvas-convergence source checks for terminals.js."""

from __future__ import annotations

import re
from pathlib import Path


TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)
CANVAS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "canvas.js"


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _canvas_src() -> str:
    return CANVAS_JS.read_text(encoding="utf-8")


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
    assert re.search(r"\bfunction\s+termOpenPty\s*\(", src) is None
    assert re.search(r"\btermOpenPty\s*\(", src) is None


def test_terminals_shared_pty_token_cache_and_send_payload():
    src = _src()
    send_body = _slice_function(src, "function termSendToCanvas(")
    assert '"dash.ptyTokens.v1"' in src
    assert "window.PtyTokens" in src
    assert "termRememberPtyToken" in src
    assert "termLookupPtyToken" in src
    assert 'kind === "terminal"' in send_body
    assert "termLookupPtyToken(key)" in send_body
    assert "openMsg.meta = meta" in send_body
    assert "openMsg.initialCommand = opts.initialCommand" in send_body


def test_start_conversation_shell_routes_pty_to_canvas_with_token_and_steps():
    body = _slice_function(_src(), "const startConversation = async ()")
    shell_pos = body.find('if (typeSelected.kind === "shell")')
    post_pos = body.find('postJson("/api/ptys"', shell_pos)
    open_pos = body.find('window.open("app/canvas.html", "dash-canvas")', shell_pos)
    assert shell_pos != -1 and post_pos != -1 and open_pos != -1
    assert open_pos < post_pos, "canvas must be opened in the user gesture before awaiting PTY creation"
    assert "termRememberPtyToken(res.id, res.token)" in body
    assert 'termSendToCanvas(_statusRowTerm("terminal", res.id)' in body
    assert "meta: { token: res.token }" in body
    assert "initialCommand: steps" in body
    assert "termOpenPty(" not in body


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
    success_pos = body.find('termSendToCanvas(_statusRowTerm("chat-codex", res.id))')
    catch_pos = body.find("} catch (err) {", success_pos)
    load_pos = body.find("await loadJobs()", catch_pos)
    assert load_pos > catch_pos, "loadJobs must run outside the start-success try/catch"


def test_canvas_terminal_open_stashes_token_and_threads_initial_command_once():
    src = _canvas_src()
    dispatch_body = _slice_function(src, "async function dispatchBusMessage(")
    render_body = _slice_function(src, "renderTree(lookup)")
    assert '"dash.ptyTokens.v1"' in src
    assert "window.PtyTokens" in src
    assert "msg.meta && msg.meta.token" in dispatch_body
    assert "canvasRememberPtyToken(key, msgToken)" in dispatch_body
    assert "window._PTY_TOKENS[id] = token" in src
    assert "INITIAL_CMD_BY_KEY[key] = msg.initialCommand" in dispatch_body
    assert "mountOpts.initialCommand = INITIAL_CMD_BY_KEY[key]" in render_body
    assert "delete INITIAL_CMD_BY_KEY[key]" in render_body
    assert "window.PaneCore.mount(container, mountOpts, CANVAS_PANE_HOST)" in render_body


def test_canvas_terminal_close_prunes_pty_token_cache():
    src = _canvas_src()
    helper_body = _slice_function(src, "function canvasForgetPtyToken(")
    close_body = _slice_function(src, "\n  closePane(key)")
    render_body = _slice_function(src, "renderTree(lookup)")
    assert "delete window._PTY_TOKENS[key]" in helper_body
    assert "window.PtyTokens.remove(key)" in helper_body
    assert 'KIND_BY_KEY[k] === "terminal"' in close_body
    assert "canvasForgetPtyToken(k)" in close_body
    assert 'KIND_BY_KEY[key] === "terminal"' in render_body
    assert "canvasForgetPtyToken(key)" in render_body


def test_legacy_v1_persistence_removed():
    src = _src()
    assert "migrateOpenPanesV1ToV2" not in src
    assert "dash.openPanes.v1" not in src
