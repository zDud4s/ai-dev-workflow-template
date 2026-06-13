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


def test_launcher_terminal_creates_pty_and_launched_row():
    # The launcher's Terminal path creates the PTY now and stashes a launched
    # row (token + the tool launch steps). It does NOT open the canvas itself —
    # the operator opens the row with ⊞. The steps run on open.
    body = _slice_function(_src(), "function termOpenDraft(")
    assert 'postJson("/api/ptys"' in body
    assert "termRememberPtyToken(res.id, res.token)" in body
    assert "termDraftLaunchCommand(tool, model" in body
    assert 'kind: "terminal"' in body
    assert "token: res.token" in body
    assert "steps" in body
    assert "addLaunched(" in body
    assert "termSendToCanvas(" not in body, "launch must not open the canvas itself"
    assert "termOpenPty(" not in body


def test_launcher_ai_chat_launches_pending_session_no_first_turn():
    # Launching an AI chat mints a sid + adds a pending session row, and does
    # NOT post the first turn (decoupled — the message is typed on the canvas).
    body = _slice_function(_src(), "function termOpenDraft(")
    assert "termMintSid()" in body
    assert 'kind: "session"' in body
    assert "addLaunched(" in body
    assert "/input" not in body, "launch must not POST the first turn"
    assert "termSendToCanvas(" not in body


def test_open_launched_materialises_on_canvas():
    body = _slice_function(_src(), "function openLaunched(")
    assert 'e.kind === "terminal"' in body
    assert 'termSendToCanvas(_statusRowTerm("terminal", e.id)' in body
    assert "initialCommand" in body
    assert "termRouteSessionToCanvas(e.id)" in body
    # Keeps the launched row (re-openable, "on canvas" badge) — does not drop it.
    assert "removeLaunched(id)" not in body


def test_canvas_session_pane_rearms_stream_after_first_send():
    # A launched session opened on the canvas 404s until its first turn creates
    # the transcript; paneCoreSendSession must re-arm the stream after the turn
    # is accepted so the conversation renders.
    body = _slice_function(_pane_core_src(), "async function paneCoreSendSession(")
    assert "accepted" in body
    assert "t.openStream()" in body
    assert "_sessReconnectStopped = false" in body


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


def test_launcher_codex_launches_as_shell_not_job():
    # Codex direct-chat (the old chat-codex job posted with a first message) is
    # gone — codex now launches as a real shell running `codex`.
    body = _slice_function(_src(), "function termOpenDraft(")
    assert '{ kind: "chat-codex"' not in body
    assert 'postJson("/api/jobs"' not in body
    assert 'postJson("/api/ptys"' in body


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


def test_canvas_window_reused_not_reloaded():
    """Regression (two bugs): (1) opening the canvas a second time must NOT
    re-navigate the named window — a LIVE _CANVAS_WIN handle is reused via
    .focus() so adding a pane never reloads the canvas; (2) a fresh open must
    load canvas.html by an ABSOLUTE url, never strand on about:blank (the old
    empty-url-then-relative-navigate trick did). canvasOpenWindow owns this; the
    callers route through it."""
    src = _src()
    co = _slice_function(src, "function canvasOpenWindow(")
    assert "_CANVAS_WIN" in co
    # (1) Live handle reused via focus, returned without re-navigation.
    assert "!_CANVAS_WIN.closed" in co
    assert ".focus()" in co
    # (2) No handle but a canvas is alive → reacquire WITHOUT navigating (empty
    # url focuses the live window, no reload, so the in-flight `open` survives).
    assert "isStale" in co
    assert 'window.open("", "dash-canvas")' in co
    # (3) Create path uses an ABSOLUTE url so it never resolves against an
    # about:blank base (the prior about:blank bug).
    assert 'new URL("app/canvas.html", window.location.href)' in co
    assert 'window.open(canvasUrl, "dash-canvas")' in co
    # termSendToCanvas must not bare-open the canvas URL itself — routes through
    # canvasOpenWindow.
    send_body = _slice_function(src, "function termSendToCanvas")
    assert 'window.open(' not in send_body
    assert "canvasOpenWindow()" in send_body


def test_canvas_2x2_grid_cap_and_placement():
    """The canvas tiles panes into a 2x2 grid and rejects a fifth pane."""
    src = _canvas_src()
    assert "CANVAS_MAX_PANES = 4" in src
    assert "function canvasPlacementFor" in src
    assert "function canvasFlashNote" in src
    # The open handler enforces the cap and uses the placement helper.
    assert "ST.keys(TREE).length >= CANVAS_MAX_PANES" in src
    assert "canvasPlacementFor(TREE)" in src


def test_auto_open_transcript_mirror_removed():
    """The poll-driven external-IDE-transcript auto-mirror is gone — it flooded
    the canvas with every recently-touched session (incl. the one driving the
    dashboard). The Terminals tab is a manual launcher now."""
    src = _src()
    assert "function termAutoOpenActiveTranscripts" not in src
    assert "termAutoOpenActiveTranscripts()" not in src


def test_canvas_does_not_restore_panes_on_boot():
    """Reopening the canvas must come up CLEAN — only what the operator opens
    now, not a resurrected snapshot. restoreCanvasState no longer deserialises /
    re-mounts the saved tree; it drops it. beforeunload also marks the heartbeat
    dead and clears the tree so a fast close→reopen reopens fresh."""
    src = _canvas_src()
    restore = _slice_function(src, "function restoreCanvasState")
    # No pane rehydration: the saved tree is neither deserialised nor mounted.
    assert "SplitTree.deserialize" not in restore
    assert "PaneCore.fetchMeta" not in restore
    assert "CanvasApp.setTree" not in restore
    # It drops the persisted tree instead.
    assert "state.tree = null" in restore
    # beforeunload marks the window dead immediately so canvasOpenWindow reopens.
    assert "state.lastSeen = 0" in src
