"""Static-lint tests for Claude session routing in terminals.js.

The dashboard no longer builds inline non-PTY panes. New Claude turns create a
session with a direct /api/sessions/<sid>/input POST and then route the session
key to the standalone canvas window.
"""

from pathlib import Path


TERMINALS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"


def js() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function/closure whose signature matches ``header``."""
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


def test_inline_session_and_job_builders_removed():
    src = js()
    assert "function termOpenSession" not in src
    assert "function termSendSession" not in src
    assert "function termOpen(" not in src
    assert "function termOpenDispatchTracker" not in src


def test_session_endpoints_and_state_handlers_remain():
    src = js()
    assert "/api/sessions/" in src
    assert "/stream" in src
    assert "/input" in src
    assert "state_change" in src
    assert "acquiring" in src
    assert "engine" in src
    assert '"foreign"' in src
    assert '"warning"' in src


def test_new_claude_chat_posts_first_turn_and_routes_canvas():
    src = js()
    body = _slice_function(src, "const startConversation = async ()")
    assert "randomUUID" in body
    assert 'window.open("app/canvas.html", "dash-canvas")' in body
    assert '"/api/sessions/" + encodeURIComponent(sid) + "/input"' in body
    assert "owner: termClientId()" in body
    assert "payload.model = model" in body
    assert 'termSendToCanvas(_statusRowTerm("session", "session:" + sid))' in body
    assert "termOpenSession(" not in body
    assert "termSendSession(" not in body


def test_codex_chat_routes_job_to_canvas():
    src = js()
    body = _slice_function(src, "const startConversation = async ()")
    assert '{ kind: "chat-codex", task: text, model }' in body
    assert 'termSendToCanvas(_statusRowTerm("chat-codex", res.id))' in body
    assert "termOpen(res.id" not in body


def test_picker_unified_sessions_group():
    src = js()
    assert "/api/sessions" in src, "picker should fetch /api/sessions"
    assert 'value="session:' in src, "picker should emit session:<sid> option values"
    assert "s.state" in src or ".state" in src, "picker should show a per-session state chip"


def test_picker_jobs_group_excludes_claude_chat():
    src = js()
    assert 'kind !== "chat"' in src, "Jobs group should exclude kind=='chat' (now sessions)"


def test_picker_open_routes_session_to_canvas():
    src = js()
    body = _slice_function(src, '$("#term-open")?.addEventListener("click", async ()')
    assert 'source === "session"' in body
    assert "termRouteSessionToCanvas(id)" in body
    assert "termOpenSession(" not in body


def test_persistence_is_canvas_owned_with_legacy_pty_token_migration():
    src = js()
    assert "dash.ptyTokens.v1" in src
    assert "dash.openPanes.v2" in src
    assert "dash.openPanes.v1" not in src
    assert "migrateOpenPanesV1ToV2" not in src
    persist_body = _slice_function(src, "function persistOpenPanes(")
    assert "Canvas owns durable pane layout now" in persist_body
    assert "localStorage.setItem(PERSIST_KEY" not in persist_body


def test_restore_only_migrates_legacy_pty_tokens():
    src = js()
    body = _slice_function(src, "async function restoreOpenPanes(")
    assert "termRememberPtyToken(id, saved.tokens[id])" in body
    assert 'fetch("/api/ptys/"' not in body
    assert "termOpenPty(" not in body
    assert "termOpenSession(" not in body
    assert "termOpen(" not in body


def test_legacy_symbols_removed():
    src = js()
    assert "function forkAndSend" not in src
    assert "function termSendResumeChat" not in src and "termSendResumeChat(" not in src
    assert "function termOpenTranscript" not in src and "termOpenTranscript(" not in src


def test_term_send_still_exists_as_codex_dispatcher():
    src = js()
    assert "function termSend" in src, "termSend must remain (codex dispatcher)"
    assert "termSendCodexNextTurn(" in src, "termSend must still dispatch chat-codex"


def test_no_transcript_kind_branches_remain():
    src = js()
    assert 'kind === "transcript"' not in src


def test_picker_sessions_group_excludes_codex():
    src = js()
    assert 's.kind !== "chat-codex"' in src, "Sessions group must filter out chat-codex"
