"""Static-lint tests for the remaining HIGH terminals.js fixes.

Pattern modeled on ``tests/test_terminals_fixes.py`` — the dashboard has no
jsdom harness, so we assert on source-level invariants that prove the fixes
are present.

Covers six fixes:
  1. termSetDead must mutate the status pill IN PLACE (no outerHTML =).
  2. The /api/skills 5s cache must be invalidated when a fetch fails so the
     popup doesn't keep replaying a stale skill set after the server starts
     returning errors.
  3. The composer autocomplete pick handler must re-read input.value /
     input.selectionStart at click time and abort if they drifted from the
     closure-captured snapshot — otherwise the splice corrupts the textarea.
  4. termRenderJsonObject must scope its .task lookup to ``.term-head .task``
     so a tool result rendering nested ``.task`` markup doesn't get renamed.
  5. The chat-pane SSE wiring must track a heartbeat timestamp so a
     Firefox-style readyState-0-forever disconnect is detected and closed.
  6. Multiple ``postJson(...)`` catch sites must surface a setMsg toast so
     the operator sees that the click did not actually land.
"""

from pathlib import Path

TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function whose signature matches ``header``."""
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


# ----- Fix 1: termSetDead in-place mutation -----


def test_termSetDead_uses_in_place_mutation():
    """termSetDead must mutate the existing .status-pill node, not replace
    it via ``outerHTML =``. PTY closures (ws.onopen/onmessage/onerror) and
    the chat SSE wiring capture the original element reference — replacing
    the node detaches them and every subsequent termSetPillState call from
    those closures mutates an orphan."""
    body = _slice_function(_src(), "function termSetDead(")
    # No outerHTML reassignment on the status pill.
    assert "status.outerHTML" not in body, (
        "termSetDead must not reassign status.outerHTML — PTY/SSE closures "
        "capture the pill reference at pane-open and would silently target "
        "an orphaned node forever after."
    )
    # And the in-place mutation idioms must be present.
    assert ("classList.remove" in body) or ("className =" in body), (
        "termSetDead must mutate the pill in place: classList.remove(...) "
        "or className = '...' so the captured node keeps its identity."
    )
    assert "textContent" in body, (
        "termSetDead must set textContent on the existing pill instead of "
        "regenerating its inner markup."
    )


# ----- Fix 2: Skills cache invalidated on error -----


def test_skills_cache_invalidated_on_error():
    """The 5s _SKILLS_CACHE must drop its cached payload when a fetch fails
    so the popup doesn't replay a stale skill set across error windows."""
    src = _src()
    # Locate the skills-branch of termHandleComposerInput. We look at the
    # block from `if (trigger === "/")` to the matching `} else {`.
    open_idx = src.find('if (trigger === "/")')
    assert open_idx != -1, "skills autocomplete branch not found"
    # Use a generous window large enough to cover the whole branch.
    window = src[open_idx : open_idx + 3000]
    # An invalidation pattern must appear somewhere in the error/failure
    # paths of this branch. Accept either the explicit reset-to-null shape
    # we wrote or any clear ``_SKILLS_CACHE = { ... data: null ...`` form.
    assert "_SKILLS_CACHE = { at: 0, data: null }" in window or (
        "_SKILLS_CACHE" in window and "data: null" in window
    ), (
        "termHandleComposerInput must invalidate _SKILLS_CACHE on fetch "
        "failure so the popup re-queries instead of serving a stale snapshot."
    )


# ----- Fix 3: Composer pick re-checks caret -----


def test_composer_pick_rechecks_caret():
    """At click time, the pick handler must re-read input.value and
    input.selectionStart and abort if they no longer match the closure-
    captured ``val``/``caret`` — otherwise the splice corrupts the textarea
    when the operator kept typing after the popup opened."""
    src = _src()
    body = _slice_function(src, "async function termHandleComposerInput(")
    # The re-reads must be present.
    assert "input.value" in body and "input.selectionStart" in body, (
        "pick handler must re-read input.value and input.selectionStart"
    )
    # And there must be at least one explicit abort/return guard against
    # the captured snapshot. We look for the assignment-style guard.
    assert "curVal !== val" in body or "curCaret !== caret" in body, (
        "pick handler must compare the freshly-read textarea state against "
        "the captured val/caret and abort the splice on mismatch."
    )


# ----- Fix 4: pane.querySelector(".task") scoped to head -----


def test_pane_task_query_is_scoped():
    """termRenderJsonObject must NOT do an unscoped ``querySelector(".task")``
    — a tool result rendering nested ``.task`` markup would otherwise grab
    the first match and the title would silently rename the wrong thing."""
    body = _slice_function(_src(), "function termRenderJsonObject(")
    assert 'querySelector(".task")' not in body, (
        "termRenderJsonObject must scope its .task lookup (e.g. "
        "'.term-head .task') instead of using a bare 'querySelector(\".task\")'"
    )
    # And the scoped form should be present at least once.
    assert (".term-head .task" in body) or (":scope > .term-head .task" in body), (
        "termRenderJsonObject must use a scoped selector when looking up the "
        "header task element"
    )


# ----- Fix 5: SSE heartbeat present -----


def test_sse_heartbeat_present():
    """A heartbeat timestamp must be tracked on the term object so the
    Firefox half-open EventSource case (readyState stuck at 0) is detected
    and the pane is forced into the close path."""
    src = _src()
    assert "_lastSSEEvent" in src, (
        "chat SSE wiring must stamp t._lastSSEEvent (or similar) on each "
        "event to detect stale connections."
    )
    # And there must be a setInterval-style watchdog referencing it.
    assert "setInterval" in src, (
        "SSE wiring must run an interval-based watchdog that consults the "
        "heartbeat timestamp."
    )
    # The interval handle must be cleaned up somewhere (termClose et al.)
    assert "clearInterval" in src, (
        "the heartbeat interval must be torn down (clearInterval) when the "
        "pane closes — otherwise it leaks a timer per closed pane."
    )


# ----- Fix 6: postJson errors surface a toast -----


def test_postjson_errors_show_msg():
    """At least two ``catch`` blocks in terminals.js must invoke ``setMsg(``
    so failed postJson clicks (cancel / stop / fork / send / kill) surface
    a toast instead of silently swallowing the error."""
    src = _src()
    # Scan every catch (whether `catch (e)`, `catch (err)`, or `.catch(`) and
    # check the immediate body for a setMsg call.
    idx = 0
    matches = 0
    while True:
        nxt = src.find("catch", idx)
        if nxt == -1:
            break
        brace = src.find("{", nxt)
        if brace == -1:
            break
        # Walk the matching brace.
        depth = 0
        end = brace
        for i in range(brace, len(src)):
            ch = src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        block = src[brace : end + 1]
        if "setMsg(" in block:
            matches += 1
        idx = end + 1
    assert matches >= 2, (
        f"expected at least 2 catch blocks containing setMsg(...) for "
        f"operator-visible toasts on failed postJson clicks; found {matches}"
    )
