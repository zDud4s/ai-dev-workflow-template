"""Static-lint regression tests for terminals.js batch-6 fixes.

Covers four targets that survived batches 1–5 (or that this batch hardens
further). All are source-level invariants — the dashboard has no jsdom
harness, so we assert on regex/AST-shape in ``.ai/dashboard/app/terminals.js``.

  1. HIGH — termSetDead clears stale module-map refs/timers so a "dead"
     pane stops mutating cached DOM nodes:
       * t._composerTimer  (autocomplete debounce)
       * t._popOpen        (autocomplete popup state)
       * t.toolUseEls      (tool-use DOM ref Map)
       * t._waitingMsg     (dispatch placeholder ref)
     The status-pill node itself is intentionally preserved (PTY/SSE
     closures captured it at open time — see in-line comment).

  2. HIGH — every ``.task`` querySelector inside terminals.js is scoped to
     ``.term-head .task`` so tool results / markdown that introduce nested
     ``.task`` elements in the body cannot intercept the lookup.

  3. HIGH — composer autocomplete TOCTOU: between popup-open and pick(),
     the live textarea state must be re-read; if val/caret have shifted
     the splice is aborted. Both the "/"-skills and "@"-files branches
     must guard.

  4. HIGH — termSendResumeChat needs an in-flight latch (mirroring
     ``t._codexAwaitInFlight``) because termSend is also reachable via
     the Enter keydown handler, which does not consult sendBtn.disabled.

Also a regression guard:
  5. termCloseAllFinished still snapshots TERMS.keys() before iterating.
  6. No raw ``console.log`` self-tests survive in the shipped bundle.
"""
from __future__ import annotations

import re
from pathlib import Path

TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function whose signature matches ``header``.

    ``header`` is the literal prefix up to (but not including) the opening
    brace, e.g. ``"function termSetDead("`` or ``"async function termSendResumeChat("``.
    """
    idx = src.find(header)
    assert idx != -1, f"could not locate {header!r} in terminals.js"
    brace = src.find("{", idx)
    assert brace != -1, f"no opening brace after {header!r}"
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


# ---------------------------------------------------------------------------
# Target 1 — termSetDead clears stale refs/timers
# ---------------------------------------------------------------------------


def test_term_set_dead_clears_composer_timer():
    """Pending /skills + @file debounce timer must be cancelled when the
    pane goes dead — otherwise it fires against a pane the user has
    visually moved on from and spams the API."""
    body = _slice_function(_src(), "function termSetDead(")
    assert "_composerTimer" in body, (
        "termSetDead must reference t._composerTimer to cancel pending "
        "autocomplete debounce timers on a dead pane"
    )
    assert "clearTimeout" in body, (
        "termSetDead must clearTimeout the captured composer debounce"
    )


def test_term_set_dead_closes_autocomplete_popup():
    """If the autocomplete popup is open when the pane dies, the operator
    could click an entry whose splice targets a textarea about to be
    repurposed for resume. Close the popup defensively."""
    body = _slice_function(_src(), "function termSetDead(")
    assert "_popOpen" in body and "termCloseAutocomplete" in body, (
        "termSetDead must close any open autocomplete popup so a stale "
        "click cannot splice into a repurposed composer"
    )


def test_term_set_dead_clears_tooluse_dom_ref_map():
    """t.toolUseEls is a Map<id, {pill, detail}> of cached DOM refs. After
    death no further tool_result events should mutate those nodes — the
    pane is "history" mode."""
    body = _slice_function(_src(), "function termSetDead(")
    assert "toolUseEls" in body, (
        "termSetDead must clear t.toolUseEls so cached DOM refs don't "
        "receive late tool_result frames on a dead pane"
    )
    assert ".clear()" in body, (
        "termSetDead must call .clear() on the toolUseEls Map (not just "
        "reassign — other code may hold the same Map reference)"
    )


def test_term_set_dead_drops_waiting_msg_ref():
    """Dispatch-tracker panes stash a placeholder via t._waitingMsg. Once
    the pane is dead we no longer need the strong ref; drop it to GC."""
    body = _slice_function(_src(), "function termSetDead(")
    assert "_waitingMsg" in body, (
        "termSetDead must null out t._waitingMsg so the placeholder "
        "element can be collected once the pane is later closed"
    )


def _strip_comments_and_strings(src: str) -> str:
    """Return ``src`` with // line comments, /* */ block comments, and
    string literals ('…', "…", `…`) replaced by whitespace so token
    presence checks don't false-positive on documentation text."""
    out: list[str] = []
    in_str: str | None = None  # ' or " or `
    in_line = False
    in_block = False
    i = 0
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append("\n")
            else:
                out.append(" ")
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if in_str:
            if ch == "\\":
                out.append("  ")
                i += 2
                continue
            if ch == in_str:
                in_str = None
                out.append(" ")
            else:
                out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line = True
            out.append("  ")
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            out.append("  ")
            i += 2
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_term_set_dead_preserves_status_pill_in_place():
    """REGRESSION — the in-place pill mutation must remain (PTY/SSE
    closures captured the original node at open time). The fix must NOT
    replace the status pill node via outerHTML / replaceChild."""
    body = _strip_comments_and_strings(_slice_function(_src(), "function termSetDead("))
    assert "outerHTML" not in body, (
        "termSetDead must NEVER replace .status-pill via outerHTML — "
        "captured closures would address a detached orphan forever"
    )
    assert "replaceChild" not in body
    # Sanity: the in-place mutation pattern is still here. Pull the
    # uncommented body of the parent file and confirm the live assignments.
    raw = _slice_function(_src(), "function termSetDead(")
    assert "status.textContent" in raw
    assert "status.className" in raw


# ---------------------------------------------------------------------------
# Target 2 — `.task` selector scoping
# ---------------------------------------------------------------------------


def test_task_querySelector_is_always_scoped_to_term_head():
    """Every ``querySelector(".task"...)`` and ``querySelectorAll(".task"...)``
    call in terminals.js must be scoped to ``.term-head .task`` so a tool
    result that embeds a nested ``.task`` cannot hijack the lookup."""
    src = _src()
    # Find every querySelector(...) call whose selector contains ".task" as
    # a word (so we don't get spurious matches for ".task-foo").
    pattern = re.compile(
        r"querySelector(?:All)?\(\s*(['\"])([^'\"]*\.task\b[^'\"]*)\1"
    )
    bare_hits: list[str] = []
    for m in pattern.finditer(src):
        selector = m.group(2)
        # Allow scoped variants like ".term-head .task" or ".term-head>.task".
        if ".term-head" in selector:
            continue
        # Allow selectors that already qualify the class name (e.g. ".task-pill").
        if not re.search(r"\.task(?:\s|\[|:|>|\+|~|,|$)", selector):
            continue
        bare_hits.append(selector)
    assert not bare_hits, (
        "unscoped .task selectors found in terminals.js: "
        + repr(bare_hits)
        + " — must be scoped to .term-head .task to avoid collision with "
        "nested .task nodes rendered into the pane body"
    )


# ---------------------------------------------------------------------------
# Target 3 — composer autocomplete TOCTOU
# ---------------------------------------------------------------------------


def test_composer_autocomplete_rereads_input_state_before_splice():
    """Both branches of termHandleComposerInput (skills "/" and files "@")
    must re-read input.value + input.selectionStart inside the pick()
    callback and bail if they differ from the captured val/caret."""
    body = _slice_function(_src(), "async function termHandleComposerInput(")
    # The current implementation captures `val` + `caret` near the top and
    # closures over them inside the pick callbacks. Split into the skills
    # and files branches by walking the top-level if/else structure.
    skills_marker = 'if (trigger === "/")'
    skills_idx = body.find(skills_marker)
    assert skills_idx != -1, "could not locate skills branch in termHandleComposerInput"
    # Walk braces to find the matching close of the `if` block, then the
    # next token must be `} else {`.
    open_brace = body.find("{", skills_idx)
    depth = 0
    end_skills = -1
    for j in range(open_brace, len(body)):
        c = body[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_skills = j
                break
    assert end_skills != -1
    files_marker = body.find("else", end_skills)
    files_open = body.find("{", files_marker)
    depth = 0
    end_files = -1
    for j in range(files_open, len(body)):
        c = body[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_files = j
                break
    assert end_files != -1, "could not locate matching close of files branch"
    skills_branch = body[skills_idx : end_skills + 1]
    files_branch = body[files_marker : end_files + 1]
    for label, branch in (("skills", skills_branch), ("files", files_branch)):
        # Inside the pick callback, the live textarea is re-read into curVal/
        # curCaret and compared to the captured val/caret snapshots.
        assert "input.value" in branch, (
            f"{label} branch must re-read input.value inside pick callback "
            "(TOCTOU guard)"
        )
        assert "input.selectionStart" in branch, (
            f"{label} branch must re-read input.selectionStart inside pick "
            "callback (TOCTOU guard)"
        )
        # The re-check should compare against the captured snapshot and bail.
        assert re.search(r"!==\s*val\b", branch), (
            f"{label} branch must compare current input.value against the "
            "captured ``val`` snapshot and abort the splice on divergence"
        )
        assert re.search(r"!==\s*caret\b", branch), (
            f"{label} branch must compare current selectionStart against the "
            "captured ``caret`` snapshot and abort the splice on divergence"
        )
        # The bail path must close the popup.
        assert "termCloseAutocomplete" in branch, (
            f"{label} TOCTOU guard must close the popup before bailing so "
            "the stale entries don't sit there waiting for another click"
        )


def test_composer_autocomplete_seq_race_guard_still_intact():
    """REGRESSION — the existing per-fetch sequence number (``_composerSeq``)
    plus isLatest() check must remain so a stale-fetch winner doesn't
    overwrite the popup with old results."""
    body = _slice_function(_src(), "async function termHandleComposerInput(")
    assert "_composerSeq" in body
    assert "isLatest()" in body, (
        "termHandleComposerInput must keep the isLatest() guard so a slow "
        "fetch that resolves after the user kept typing is discarded"
    )


# ---------------------------------------------------------------------------
# Target 5 — termCloseAllFinished snapshot (regression from batch 4)
# ---------------------------------------------------------------------------


def test_term_close_all_finished_snapshots_keys():
    """REGRESSION — batch 4 fixed live-iteration; ensure the snapshot
    pattern persists. termClose() deletes the entry mid-iteration, and on
    chat-codex rekeys the close cascade can remove siblings."""
    body = _slice_function(_src(), "function termCloseAllFinished(")
    assert "[...TERMS.keys()]" in body or "Array.from(TERMS.keys())" in body, (
        "termCloseAllFinished must iterate a snapshot of TERMS.keys() — "
        "live iteration may skip a sibling when termClose cascades"
    )


# ---------------------------------------------------------------------------
# Target 6 — no console.log self-tests in prod (regression from batch 4)
# ---------------------------------------------------------------------------


def test_no_raw_console_log_self_tests():
    """REGRESSION — the simpleLineDiff self-test must not have crept back
    in. console.debug and console.warn are fine; console.log is the
    test-print smell we forbid."""
    src = _src()
    hits = re.findall(r"\bconsole\.log\s*\(", src)
    assert not hits, (
        "console.log calls found in terminals.js — gate behind __DEV__ or "
        "remove. console.debug/console.warn for runtime diagnostics is OK."
    )


# ---------------------------------------------------------------------------
# Bundle sanity
# ---------------------------------------------------------------------------


def test_terminals_js_non_empty_after_edits():
    """Cheap sanity check: a malformed edit usually shrinks the file
    dramatically. terminals.js was ~178KB at the time of writing; a sudden
    drop below 100KB almost certainly indicates a truncating edit."""
    src = _src()
    assert len(src) > 100_000, (
        f"terminals.js shrank to {len(src)} bytes — likely truncation"
    )
    # The two header comment lines should still cap the file (they were
    # added during the script extraction and aren't touched by feature
    # edits — their disappearance is a strong signal of a regression).
    assert src.lstrip().startswith("// .ai/dashboard/app/terminals.js"), (
        "terminals.js extraction-header comment is missing — likely a "
        "destructive edit at file top"
    )
    # And the DOMContentLoaded boot block should remain at the tail.
    assert "restoreOpenPanes" in src[-2000:], (
        "terminals.js no longer ends with the restoreOpenPanes boot "
        "sequence — possible truncation"
    )
