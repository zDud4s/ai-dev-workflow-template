"""Static-lint tests for the SessionPane frontend (Task 9).

Asserts that terminals.js contains the two new top-level functions
(termOpenSession / termSendSession) and that they reference the correct
unified API endpoints and session-state vocabulary introduced in Tasks 5-8.
No JS runtime is available; all checks are source-level string assertions,
matching the established pattern in tests/test_terminals_medium.py.
"""

from pathlib import Path

TERMINALS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"


def js():
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


def test_has_session_pane_open_and_send():
    src = js()
    assert "function termOpenSession" in src
    assert "function termSendSession" in src


def test_session_pane_uses_unified_endpoints():
    src = js()
    assert "/api/sessions/" in src
    assert "/stream" in src
    assert "/input" in src


def test_session_pane_consumes_state_change_and_states():
    src = js()
    assert "state_change" in src            # consumes the SessionEvent state frames
    assert "acquiring" in src               # new state strings the chip switches on
    assert "engine" in src


def test_session_pane_send_is_wired():
    src = js()
    # composer always-on path: send goes through termSendSession, not the old fork gate
    assert "termSendSession" in src
    assert "termOpenSession" in src


def test_session_pane_closes_stream_on_collapse():
    src = js()
    # session panes must participate in the lazy-stream collapse lifecycle
    # (close EventSource on collapse) like transcript panes, to avoid leaking
    # one of the browser's ~6 HTTP/1.1 connections per collapsed pane.
    assert "closeStream" in src
    # termSetCollapsed must handle the session kind, not only transcript
    import re as _re
    # crude but effective: the collapse handler references "session" alongside the stream toggle
    assert _re.search(r'kind\s*===\s*"session"', src), "termSetCollapsed should handle kind === 'session'"


# ------------------------------------------------------------------
# Task A: foreign chip, queued suffix, warning rendering
# ------------------------------------------------------------------

def test_chip_foreign_state_and_external_label():
    src = js()
    # termSessionChipUpdate must have a branch for the "foreign" state
    assert '"foreign"' in src, 'termSessionChipUpdate should branch on state === "foreign"'
    assert '"external"' in src, 'the foreign-state pill label should be "external"'


def test_chip_queued_suffix():
    src = js()
    # When t.pending is true the chip label gets a " · queued" suffix
    assert "t.pending" in src, "termSessionChipUpdate should read t.pending"
    assert '"queued"' in src or "queued" in src, 'a "queued" label/suffix must appear'


def test_handle_session_event_stores_pending():
    src = js()
    # state_change handler must also store t.pending = !!ev.pending
    assert "t.pending" in src and "ev.pending" in src, (
        "termHandleSessionEvent should store t.pending = !!ev.pending on state_change"
    )


def test_handle_session_event_warning_kind():
    src = js()
    # termHandleSessionEvent must handle kind === "warning" frames
    assert '"warning"' in src, 'termHandleSessionEvent should branch on kind === "warning"'


# ------------------------------------------------------------------
# Task B: queue-aware send — explicit 202 and 409 branches
# ------------------------------------------------------------------

def test_send_session_inspects_status_202():
    src = js()
    assert "202" in src, "termSendSession should handle HTTP 202 (queued) explicitly"


def test_send_session_inspects_status_409():
    src = js()
    assert "409" in src, "termSendSession should handle HTTP 409 (already_queued) explicitly"


# ------------------------------------------------------------------
# Task C: release + interrupt controls in pane header
# ------------------------------------------------------------------

def test_open_session_has_release_control():
    src = js()
    assert '"/release"' in src or "'/release'" in src or "/release" in src, (
        "termOpenSession should wire a release fetch to /api/sessions/<sid>/release"
    )


def test_open_session_has_interrupt_control():
    src = js()
    assert '"/interrupt"' in src or "'/interrupt'" in src or "/interrupt" in src, (
        "termOpenSession should wire an interrupt fetch to /api/sessions/<sid>/interrupt"
    )


# ------------------------------------------------------------------
# Task D: per-tab owner id so the registry can track multi-tab ownership
# ------------------------------------------------------------------

def test_send_session_includes_owner():
    src = js()
    # The /input POST body must carry an owner field so the backend can tell
    # turns from different tabs apart (registry already accepts owner).
    assert "owner:" in src, "termSendSession should include an owner field in the /input body"


def test_owner_id_is_per_tab_stable():
    src = js()
    # The owner id must be stable for the life of the tab (survives reloads,
    # distinct per tab) — sessionStorage is per-tab, so it is the right store.
    assert "sessionStorage" in src, "owner id should be persisted per-tab via sessionStorage"


def test_queued_chip_updates_immediately_on_202():
    src = js()
    # On a 202 (queued) the chip should flip to the queued state right away
    # rather than waiting ~1s for the next SSE state frame.
    assert "t.pending = true" in src, "the 202 branch should set t.pending = true"


# ------------------------------------------------------------------
# Fase 3 — Task 4: unified picker (Sessions from /api/sessions + Jobs section)
# ------------------------------------------------------------------

def test_picker_unified_sessions_group():
    src = js()
    # The picker's session list is fed by the unified endpoint and renders a
    # state chip per row, with session:<sid> option values.
    assert "/api/sessions" in src, "picker should fetch /api/sessions"
    assert 'value="session:' in src, "picker should emit session:<sid> option values"
    assert "s.state" in src or ".state" in src, "picker should show a per-session state chip"


def test_picker_jobs_group_excludes_claude_chat():
    src = js()
    # Claude chats are sessions now; the Jobs group keeps only non-chat jobs
    # (orchestrate / plan / codex).
    assert 'kind !== "chat"' in src, "Jobs group should exclude kind=='chat' (now sessions)"


def test_picker_open_routes_session_to_session_pane():
    src = js()
    assert 'source === "session"' in src, "open handler should route session: selections"
    # and it opens the unified pane
    assert "termOpenSession(" in src


# ------------------------------------------------------------------
# Task 5: route every Claude chat through the session pane
# ------------------------------------------------------------------

def test_term_open_routes_claude_chat_to_session():
    src = js()
    body = _slice_function(src, "function termOpen(")
    assert 'kind === "chat"' in body, "termOpen should branch on the Claude chat kind"
    assert "termOpenSession(" in body, (
        "termOpen should delegate Claude chats to termOpenSession"
    )


def test_new_claude_chat_opens_session_pane():
    src = js()
    body = _slice_function(src, "const startConversation = async ()")
    assert "termOpenSession(" in body, (
        "the new Claude chat launch path should open a session pane"
    )
    assert "randomUUID" in body, (
        "the new Claude chat launch path should mint a fresh sid via crypto.randomUUID"
    )


def test_codex_chat_still_opens_via_term_open():
    src = js()
    body = _slice_function(src, "const startConversation = async ()")
    # chat-codex must continue to open via termOpen (job pane), not session.
    assert "termOpen(res.id" in body, (
        "chat-codex should still open via termOpen, not termOpenSession"
    )


# ------------------------------------------------------------------
# Task 6: branch control in the session pane header
# ------------------------------------------------------------------

def test_open_session_has_branch_control():
    src = js()
    body = _slice_function(src, "function termOpenSession(")
    # A Branch button must POST to /api/sessions/<sid>/branch.
    assert "/branch" in body, (
        "termOpenSession should wire a /branch fetch"
    )
    # On success it opens the forked sid as a fresh session pane.
    assert "termOpenSession(" in body, (
        "the branch handler should open the returned sid via termOpenSession"
    )


def test_branch_button_present_in_header():
    src = js()
    body = _slice_function(src, "function termOpenSession(")
    assert "branch-btn" in body, "the session header should render a branch button"


# ------------------------------------------------------------------
# Task 7: open-panes persistence migration v1 -> v2
# ------------------------------------------------------------------

def test_persist_key_is_v2():
    src = js()
    assert "dash.openPanes.v2" in src, "the persistence key constant should be bumped to v2"


def test_migration_path_references_both_keys():
    src = js()
    # A one-shot migration must read v1 when v2 is absent, then persist v2.
    assert "dash.openPanes.v1" in src, "migration must still reference the legacy v1 key"
    assert "dash.openPanes.v2" in src, "migration must write under the v2 key"


def test_migration_maps_transcript_and_chat_to_session():
    src = js()
    # Migrated entries become session panes: transcript ide:<sid> and Claude
    # chat both convert to {kind:"session", id:"session:"+sid}.
    assert "session:" in src
    assert 'kind: "session"' in src or 'kind:"session"' in src, (
        "migration should produce session-kind entries"
    )


def test_restore_opens_session_ids():
    src = js()
    body = _slice_function(src, "async function restoreOpenPanes(")
    assert "termOpenSession(" in body, (
        "restore should open session-kind panes via termOpenSession"
    )


# ------------------------------------------------------------------
# Task 8: legacy chat/transcript/fork paths are pruned
# ------------------------------------------------------------------

def test_legacy_symbols_removed():
    src = js()
    assert "function forkAndSend" not in src, "forkAndSend must be removed"
    assert "function termSendResumeChat" not in src and "termSendResumeChat(" not in src, (
        "termSendResumeChat must be removed"
    )
    assert "function termOpenTranscript" not in src and "termOpenTranscript(" not in src, (
        "termOpenTranscript shim and its callers must be removed"
    )


def test_term_send_still_exists_as_codex_dispatcher():
    src = js()
    # termSend stays — it routes chat-codex to termSendCodexNextTurn.
    assert "function termSend" in src, "termSend must remain (codex dispatcher)"
    assert "termSendCodexNextTurn(" in src, "termSend must still dispatch chat-codex"


def test_no_transcript_kind_branches_remain():
    src = js()
    assert 'kind === "transcript"' not in src, (
        "dead kind === 'transcript' branches must be removed"
    )


# ------------------------------------------------------------------
# Fase 3 — review fixes: model pinning (#1), codex picker leak (#2),
# migration quota safety (#5).
# ------------------------------------------------------------------

def test_new_chat_pins_selected_model():
    src = js()
    # The new-chat path stores the chosen model on the session pane...
    assert "newT.model = model" in src, "new-chat path should pin the selected model on the pane"
    # ...and termSendSession forwards it in the /input body when set.
    assert "payload.model = t.model" in src, "termSendSession should send the pane's pinned model"


def test_picker_sessions_group_excludes_codex():
    src = js()
    # Codex chats are not claude --resume sessions; they must not appear in the
    # Sessions group (they would open a broken Claude pane on a codex id).
    assert 's.kind !== "chat-codex"' in src, "Sessions group must filter out chat-codex"


def test_migration_keeps_v1_on_quota_failure():
    import re
    src = js()
    # removeItem(v1) must be nested inside the successful setItem(v2) branch, so
    # a quota failure does not wipe both keys.
    m = re.search(r"setItem\(PERSIST_KEY.*?removeItem\(LEGACY_PERSIST_KEY\).*?catch", src, re.DOTALL)
    assert m, "removeItem(LEGACY) must run only inside the setItem(PERSIST_KEY) success path"
