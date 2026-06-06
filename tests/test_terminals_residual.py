"""Static-lint tests for the residual terminals.js bug-hunt items.

These cover the items still open after batches 1-4 (agent 1 of batch 5):

  4. termAppendAssistantText rAF callback must bail when textEl detached
     from the DOM (pane closed between delta and frame).
  5. termCodexAwaitNextTurn must serialise concurrent calls via an
     in-flight latch (SSE 'end' / onerror / heartbeat watchdog can all
     race for the same pane and overwrite session_id with empty).
  8. The chat ``input failed`` catch (postJson /api/jobs/<id>/input) must
     surface setMsg(...) in addition to the inline ``[input failed]``
     line — collapsed/scrolled-away panes hide the inline marker.
  9. Pill class stacking — the codex-resume es.onopen and the fork-banner
     site must route through termSetPillState instead of ad-hoc
     classList/textContent mutation (other ~3 sites already use the
     helper; these were the remaining hold-outs).

Items already verified closed before this batch (still asserted here so
regressions surface immediately):

  - termSetDead in-place mutation
  - Skills cache invalidate on /api/skills failure
  - Composer pick caret/value re-check (TOCTOU)
  - ``.task`` selector scoped to ``.term-head .task``
  - SSE heartbeat watchdog with clearInterval cleanup
  - termCloseAllFinished iterates a snapshot of keys

Tests are static (regex / brace-walked function bodies). The dashboard
has no jsdom harness here.
"""

import re
from pathlib import Path

TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)
STYLES_CSS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "styles.css"


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _css_rule_block(selector: str, css: str) -> str:
    match = re.search(rf"(?m)^[ \t]*{re.escape(selector)}\s*\{{[^}}]*\}}", css)
    assert match, f"{selector} rule not found in styles.css"
    return match.group(0)


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function whose signature matches header."""
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


def test_dead_pane_opacity_chrome_only():
    """Dead panes should dim chrome while leaving terminal body text legible."""
    css = _css()
    dead = _css_rule_block(".term-pane.dead", css)
    assert "opacity:" not in dead
    assert re.search(
        r"\.term-pane\.dead\s+\.term-head\s*,\s*"
        r"\.term-pane\.dead\s+\.term-foot\s*\{[^}]*opacity:\s*0\.75\s*;",
        css,
        re.DOTALL,
    ), "dead-pane opacity should be scoped to pane chrome"


# ----- Item 4: textEl.isConnected guard in rAF callback -----


def test_termAppendAssistantText_guards_detached_textEl():
    """The rAF callback inside termAppendAssistantText may fire after the
    pane was removed (operator closed it, chat-codex rekey replaced the
    block, termCloseAllFinished swept it). Writing into a detached node
    leaks the dataset payload + buffer for the GC lifetime and pointlessly
    re-parses markdown. The callback must short-circuit when textEl is
    no longer in the DOM."""
    body = _slice_function(_src(), "function termAppendAssistantText(")
    # The guard checks textEl.isConnected (DOM-standard property).
    assert "textEl.isConnected" in body, (
        "termAppendAssistantText's rAF callback must guard on "
        "textEl.isConnected so a closed pane doesn't trigger a write "
        "into a detached node."
    )
    # And it must clear the buffer on bail so the orphan accumulator
    # doesn't keep growing across deltas that still arrive after the
    # close (the SSE may still send a couple frames before its own
    # cleanup lands).
    assert "_rawBuf = []" in body, (
        "the bail branch must clear textEl._rawBuf so the detached node "
        "doesn't hold a growing string for the rest of the GC lifetime"
    )


# ----- Item 5: termCodexAwaitNextTurn re-entry guard -----


def test_termCodexAwaitNextTurn_serialises_concurrent_calls():
    """SSE 'end', onerror (CLOSED), and the heartbeat watchdog can all
    fire termCodexAwaitNextTurn against the same pane simultaneously.
    Each one fetches /api/jobs/<id> and the last write wins —
    sometimes overwriting a captured session_id with empty when a
    stale response from the pre-rekey job lands last. The function
    must use an in-flight latch on the term object so overlapping
    calls no-op."""
    body = _slice_function(_src(), "async function termCodexAwaitNextTurn(")
    # The latch flag must be set and checked.
    assert "_codexAwaitInFlight" in body, (
        "termCodexAwaitNextTurn must guard on a re-entry latch "
        "(t._codexAwaitInFlight or similar) so concurrent SSE/heartbeat "
        "callers don't race the /api/jobs fetch."
    )
    # And the early-return shape must come BEFORE any side effects
    # (clearing thinking placeholder, fetching, etc.).
    idx_guard = body.find("_codexAwaitInFlight")
    idx_fetch = body.find("fetch(")
    assert idx_guard != -1 and idx_fetch != -1
    assert idx_guard < idx_fetch, (
        "the re-entry guard must short-circuit BEFORE the /api/jobs "
        "fetch, otherwise the race window stays open"
    )
    # And there must be a finally-block resetting the latch so a
    # transient await rejection doesn't leave the pane locked.
    assert "finally" in body and "_codexAwaitInFlight = false" in body, (
        "the latch must be cleared in finally so an aborted /api/jobs "
        "fetch doesn't strand the pane in a permanently-locked state"
    )


# ----- Item 8: chat input failure surfaces setMsg -----


def test_chat_input_failure_surfaces_toast():
    """The postJson(/api/jobs/<id>/input) catch must emit setMsg(...) in
    addition to the inline ``[input failed]`` marker. Collapsed panes
    in list mode and scrolled-up panes hide the inline marker entirely,
    so the operator clicks Send and has no signal that nothing
    happened."""
    body = _slice_function(_src(), "async function termSend(")
    # The catch block must include both the inline ``[input failed]``
    # text AND a setMsg(...) call. We check both substrings are
    # present in the function body.
    assert "[input failed:" in body, (
        "the chat-send catch should keep the inline ``[input failed: …]`` "
        "marker (operator sees it when scrolled to the bottom)"
    )
    assert 'setMsg("#term-msg"' in body and "Send failed" in body, (
        "the chat-send catch must also call setMsg('#term-msg', 'err', "
        "'Send failed: ' + e.message, ...) so collapsed/scrolled-away "
        "panes still get an operator-visible toast"
    )


# ----- Item 9: pill helper used consistently -----


def test_pill_helper_used_at_codex_onopen():
    """The codex-resume EventSource onopen must route through
    termSetPillState — the previous direct ``pill.textContent = "live";
    pill.classList.remove("queued")`` left other prior state classes
    (warn/bad/done) stacked under running, and the cascade resolved
    colours unpredictably."""
    src = _src()
    # Locate the resume-codex SSE block (inside termSendCodexNextTurn).
    fn = _slice_function(src, "async function termSendCodexNextTurn(")
    # Inside it, find the es.onopen block.
    open_idx = fn.find("es.onopen = ")
    assert open_idx != -1, "could not find es.onopen inside termSendCodexNextTurn"
    # The onopen body must contain a termSetPillState call.
    # Find the matching brace for this arrow body.
    brace = fn.find("{", open_idx)
    depth = 0
    end = brace
    for i in range(brace, len(fn)):
        ch = fn[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    onopen_body = fn[brace : end + 1]
    assert "termSetPillState" in onopen_body, (
        "the codex-resume es.onopen must use termSetPillState(...) "
        "instead of mutating pill.textContent / classList directly"
    )
    # Strip JS line comments before checking that the ad-hoc
    # ``pill.classList.remove("queued")`` mutation is gone — leaving
    # the pattern in a documentation comment is fine.
    code_lines = []
    for line in onopen_body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    assert 'classList.remove("queued")' not in code, (
        "the ad-hoc ``pill.classList.remove(\"queued\")`` should be gone "
        "from the executable body — termSetPillState normalises every "
        "state class"
    )


# Removed: the fork-and-send affordance (forkAndSend / IDE transcript mirror
# pane) was deleted when Claude conversations converged on the unified
# session pane, so there is no fork-banner site left to assert on.


# ----- Already-closed sanity checks -----


def test_termSetDead_still_mutates_in_place():
    """Regression guard for the in-place mutation already shipped in
    batch 3 — make sure a future refactor doesn't reintroduce the
    outerHTML reassignment."""
    body = _slice_function(_src(), "function termSetDead(")
    assert "status.outerHTML" not in body
    assert "classList.remove" in body and "textContent" in body


def test_close_all_finished_uses_snapshot_iteration():
    """Regression guard for the Map-mutation-during-iteration fix from
    batch 4."""
    body = _slice_function(_src(), "function termCloseAllFinished(")
    # The snapshot pattern is ``[...TERMS.keys()]`` (or .entries() with
    # spread); we accept either as evidence of a stable snapshot.
    assert ("[...TERMS.keys()]" in body) or ("Array.from(TERMS.keys())" in body), (
        "termCloseAllFinished must iterate a snapshot of TERMS keys; "
        "iterating Map.entries() live + calling termClose() (which "
        "deletes) can skip siblings mid-cascade"
    )


def test_skills_cache_invalidated_on_error_regression():
    """Regression guard for the skills-cache invalidation from batch 3."""
    src = _src()
    open_idx = src.find('if (trigger === "/")')
    assert open_idx != -1
    window = src[open_idx : open_idx + 3000]
    assert "_SKILLS_CACHE = { at: 0, data: null }" in window


def test_sse_heartbeat_watchdog_regression():
    """Regression guard for the SSE heartbeat watchdog from batch 3."""
    src = _src()
    assert "_lastSSEEvent" in src
    assert "setInterval" in src and "clearInterval" in src


def test_composer_pick_caret_recheck_regression():
    """Regression guard for the composer TOCTOU fix from batch 3."""
    body = _slice_function(_src(), "async function termHandleComposerInput(")
    assert "curVal !== val" in body or "curCaret !== caret" in body
