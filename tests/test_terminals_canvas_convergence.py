"""Canvas-convergence source checks for terminals.js."""

from __future__ import annotations

import re
from pathlib import Path


TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)
CANVAS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "canvas.js"
PANE_CORE_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "pane-core.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _canvas_src() -> str:
    return CANVAS_JS.read_text(encoding="utf-8")


def _pane_core_src() -> str:
    return PANE_CORE_JS.read_text(encoding="utf-8")


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


def test_canvas_session_stream_retries_transient_startup_404():
    body = _slice_function(_pane_core_src(), "function paneCoreMountSession(")
    open_pos = body.find("t.openStream = () =>")
    close_pos = body.find("t.closeStream = () =>", open_pos)
    collapsed_pos = body.find("if (opts && opts.collapsed)", close_pos)
    assert open_pos != -1 and close_pos != -1 and collapsed_pos != -1

    open_body = body[open_pos:close_pos]
    close_body = body[close_pos:collapsed_pos]
    assert re.search(r"SESSION_STREAM_RECONNECT_MAX\s*=\s*12", body)
    assert re.search(r"SESSION_STREAM_RECONNECT_DELAY_MS\s*=\s*600", body)
    assert "_sessReconnectTimer" in open_body
    assert "_sessReconnectN" in open_body
    assert "setTimeout" in open_body
    assert "t.openStream()" in open_body
    assert "EventSource.CLOSED" in open_body
    assert 'termSetPillState(statusPill, "running", "connecting")' in open_body
    assert 'termSetPillState(statusPill, "warn", "disconnected")' in open_body

    onopen_pos = open_body.find("es.onopen")
    onmessage_pos = open_body.find("es.onmessage")
    assert open_body.find("t._sessReconnectN = 0", onopen_pos, onmessage_pos) != -1
    assert open_body.find("t._sessReconnectN = 0", onmessage_pos) != -1

    end_pos = open_body.find('es.addEventListener("end"')
    error_pos = open_body.find("es.onerror")
    assert end_pos != -1 and error_pos != -1 and end_pos < error_pos
    assert "setTimeout" not in open_body[end_pos:error_pos]
    assert "clearTimeout(t._sessReconnectTimer)" in close_body


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


def test_canvas_session_empty_distinct_from_disconnect():
    """No-transcript (never-connected) → calm empty state + forget, no retry;
    a real drop after connecting → the existing 'disconnected' warn state."""
    body = _slice_function(_pane_core_src(), "function paneCoreMountSession(")
    open_pos = body.find("t.openStream = () =>")
    close_pos = body.find("t.closeStream = () =>", open_pos)
    open_body = body[open_pos:close_pos]

    # The "ever connected" flag is set on every success signal and reset on a
    # fresh open (mid-reconnect retries must NOT reset it).
    fresh_reset = open_body.find("t._sessEverConnected = false")
    assert fresh_reset != -1
    onopen_pos = open_body.find("es.onopen")
    onmessage_pos = open_body.find("es.onmessage")
    end_pos = open_body.find('es.addEventListener("end"')
    error_pos = open_body.find("es.onerror")
    assert fresh_reset < onopen_pos, "reset must live in the fresh-open guard, not a handler"
    assert open_body.find("t._sessEverConnected = true", onopen_pos, onmessage_pos) != -1
    assert open_body.find("t._sessEverConnected = true", onmessage_pos, end_pos) != -1
    assert open_body.find("t._sessEverConnected = true", end_pos, error_pos) != -1

    # Budget-exhausted branch: never-connected → calm empty state (neutral
    # "done" pill, not warn), no further retry, and forget the dead key.
    err_body = open_body[error_pos:]
    assert "if (!t._sessEverConnected)" in err_body
    assert 'termSetPillState(statusPill, "done", "empty")' in err_body
    assert 'paneCoreSetActivity(t, "no transcript", "ready")' in err_body
    assert "paneCoreSessionEmptyNote(t)" in err_body
    assert "paneCoreT_host(t).forget(t.jobId)" in err_body
    # The real-drop fallback keeps the warn "disconnected" state.
    assert 'termSetPillState(statusPill, "warn", "disconnected")' in err_body
    # The never-connected branch returns before reaching the disconnect line.
    empty_pos = err_body.find("if (!t._sessEverConnected)")
    disconnect_pos = err_body.find('termSetPillState(statusPill, "warn", "disconnected")')
    assert empty_pos < disconnect_pos

    # The calm "no transcript" note helper exists.
    assert "function paneCoreSessionEmptyNote(" in _pane_core_src()
    # The host shim exposes a guarded forget() seam (no-op on a bare host).
    host_body = _slice_function(_pane_core_src(), "function paneCoreHost(")
    assert "forget(key)" in host_body
    assert "if (host.forget) host.forget(key)" in host_body


def test_canvas_host_forget_unpersists_dead_session_key():
    """The canvas host wires a forget hook that drops a dead key from the
    persisted layout without removing the visible (live TREE) pane."""
    src = _canvas_src()
    host_body = _slice_function(src, "var CANVAS_PANE_HOST = {")
    assert "forget: function (key) { CanvasApp.forgetPane(key); }" in host_body

    forget_body = _slice_function(src, "\n  forgetPane(key)")
    # Drops the render-input maps for the key.
    assert "delete KIND_BY_KEY[k]" in forget_body
    assert "delete META_BY_KEY[k]" in forget_body
    assert "delete INITIAL_CMD_BY_KEY[k]" in forget_body
    # Rewrites the persisted snapshot to the current tree MINUS the key.
    assert "window.SplitTree.remove(TREE, k)" in forget_body
    assert "window.CanvasBus.loadState()" in forget_body
    assert "window.CanvasBus.saveState(state)" in forget_body
    # Must NOT tear down the visible pane: no live TREE mutation / renderTree.
    assert "CanvasApp.setTree" not in forget_body
    assert "CanvasApp.renderTree" not in forget_body


def test_legacy_v1_persistence_removed():
    src = _src()
    assert "migrateOpenPanesV1ToV2" not in src
    assert "dash.openPanes.v1" not in src
