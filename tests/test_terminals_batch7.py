"""Static-lint regression tests for terminals.js batch-7 MEDIUM fixes.

Scope: residual MEDIUM bugs flagged in ``docs/bug-hunt-status.md`` for
``.ai/dashboard/app/terminals.js`` after batches 1–6. Targets two
remaining items; the rest of the MEDIUM bullet list was verified as
already-fixed in earlier batches.

Targets:

  1. Fire-and-forget ``loadJobs()`` calls inside event handlers and
     close paths must catch async Promise rejections — otherwise the
     browser logs an unhelpful "Uncaught (in promise)" stack the next
     time the server hiccups during a close / SSE-end / codex rekey.
     Sites in scope:
       * ``termClose`` (fired on operator-initiated close)
       * ``termSendCodexNextTurn`` (codex SSE end + rekey)
       * the chat-pane SSE ``end`` listener inside ``termOpen``

  2. ``termClosePty`` issues a fire-and-forget ``fetch(.../kill)`` to
     evict the server-side shell. The pre-existing ``try/catch`` only
     covered the synchronous arm — an async Promise rejection (network
     drop, 4xx/5xx) silently dropped the kill on the floor with no
     console diagnostic. The fetch result must now have ``.catch()``
     chained to it so async failures are surfaced via ``console.warn``.

Also includes regression guards confirming earlier-batch fixes survive:

  3. ``termCloseAllFinished`` still snapshots ``TERMS.keys()`` before
     iteration (batch 4 fix).
  4. ``console.log`` self-test is still gone from the shipped bundle
     (batch 4 fix; the explanatory comment must remain).
  5. ``termSendResumeChat`` still propagates ``t.model`` in the resume
     POST payload (batch 4 fix).
  6. The three ad-hoc pill class mutations still route through
     ``termSetPillState`` (batch 4 fix).

Pattern mirrors ``tests/test_terminals_medium.py`` and
``tests/test_terminals_batch6_refs_collisions_latches.py``.
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


# ---------------------------------------------------------------------------
# Target 1 — fire-and-forget loadJobs() must catch async rejections
# ---------------------------------------------------------------------------


def test_termClose_loadJobs_catches_rejection():
    """The synchronous loadJobs() at the tail of termClose can reject
    asynchronously (server transient 5xx, network blip) — without a
    .catch() the browser surfaces an Uncaught (in promise) noise that
    is unrelated to the close action the operator took.
    """
    body = _slice_function(_src(), "function termClose(")
    # The call site is the tail — confirm a Promise-resolving wrapper
    # with a .catch is present (we accept Promise.resolve(loadJobs()).catch
    # or loadJobs()?.catch / .then().catch shapes).
    has_catch = bool(
        re.search(r"loadJobs\(\)\s*\)\.catch\(", body)
        or re.search(r"loadJobs\(\)\?\.catch\(", body)
        or re.search(r"loadJobs\(\)\.then\([^)]*\)\.catch\(", body)
    )
    assert has_catch, (
        "termClose's fire-and-forget loadJobs() must chain .catch to "
        "surface async rejection — otherwise the browser emits a noisy "
        "Uncaught (in promise) the next time the server hiccups during close"
    )
    # And the rejection handler should write to console.warn with the
    # "[terminals]" prefix so the failure is greppable.
    assert "[terminals] loadJobs after termClose failed" in body, (
        "termClose's loadJobs catch handler should call "
        'console.warn("[terminals] loadJobs after termClose failed: ...")'
    )


def test_termSendCodexNextTurn_loadJobs_catches_rejection():
    """Both loadJobs() calls inside termSendCodexNextTurn — the rekey
    completion and the SSE 'end' listener — must catch rejection so a
    server restart mid-codex-turn doesn't leak unhandled rejections.
    """
    body = _slice_function(_src(), "async function termSendCodexNextTurn(")
    catches = re.findall(r"loadJobs\(\)\s*\)\.catch\(", body)
    assert len(catches) >= 2, (
        f"expected >=2 loadJobs().catch wrappers inside "
        f"termSendCodexNextTurn (rekey + SSE end); found {len(catches)}"
    )
    # Diagnostic prefixes must be present.
    assert "[terminals] loadJobs after codex" in body, (
        "termSendCodexNextTurn's loadJobs catches must use the "
        '"[terminals] loadJobs after codex ..."  prefix for greppability'
    )


def test_inline_termOpen_sse_handler_removed():
    """The non-PTY inline job pane was removed when chats moved to canvas."""
    src = _src()
    assert "function termOpen(" not in src
    assert "[terminals] loadJobs after SSE end failed" not in src


# ---------------------------------------------------------------------------
# Target 2 — dashboard inline PTY cleanup retired
# ---------------------------------------------------------------------------


def test_dashboard_inline_pty_cleanup_removed():
    """PTY panes are canvas-owned; terminals.js should not retain dead cleanup."""
    src = _src()
    assert "function termOpenPty" not in src
    assert "function termClosePty" not in src
    assert "/api/ptys/${ptyId}/kill" not in src


# ---------------------------------------------------------------------------
# Regression guards (earlier-batch fixes must survive)
# ---------------------------------------------------------------------------


def test_termCloseAllFinished_still_snapshots_keys():
    """REGRESSION (batch 4) — termCloseAllFinished must keep iterating
    over a snapshot of TERMS.keys() so a cascading termClose doesn't
    skip a sibling entry mid-iteration."""
    body = _slice_function(_src(), "function termCloseAllFinished(")
    assert "[...TERMS.keys()]" in body or "Array.from(TERMS.keys())" in body, (
        "termCloseAllFinished regressed back to live iteration — must "
        "snapshot TERMS.keys() before the for-of loop"
    )


def test_no_console_log_self_test_in_prod():
    """REGRESSION (batch 4) — the simpleLineDiff DOMContentLoaded
    self-test that ran on every page load must not have crept back.
    console.warn / console.debug for runtime diagnostics is fine;
    console.log is the forbidden smell.
    """
    src = _src()
    hits = re.findall(r"\bconsole\.log\s*\(", src)
    assert not hits, (
        f"console.log calls re-appeared in terminals.js: {len(hits)} site(s). "
        "Remove or gate behind `if (window._dev)` / window.DEBUG_DIFF_SELFTEST."
    )


# Removed: chat-resume model propagation — the dead-chat resume path
# (termSendResumeChat) was deleted when Claude chats converged on the
# unified session pane.


def test_dispatch_result_pill_routes_through_helper():
    """REGRESSION (batch 4) — the dispatch-tracker result block must
    not use ``classList.toggle("done", ...)``; it must route through
    ``termSetPillState`` to clear stale running/queued classes."""
    src = _src()
    assert 'classList.toggle("done"' not in src, (
        "dispatch-tracker result regressed back to "
        'classList.toggle("done", ...) — must use termSetPillState'
    )


def test_codex_pill_paths_route_through_helper():
    """REGRESSION (batch 4) — termCodexAwaitNextTurn + the codex rekey
    connecting branch must continue using termSetPillState rather than
    ad-hoc classList.add."""
    src = _src()
    await_body = _slice_function(src, "async function termCodexAwaitNextTurn(")
    send_body = _slice_function(src, "async function termSendCodexNextTurn(")
    for label, body in (("termCodexAwaitNextTurn", await_body),
                        ("termSendCodexNextTurn", send_body)):
        assert "termSetPillState(" in body, (
            f"{label} regressed — must keep routing pill transitions "
            "through termSetPillState so stale state classes don't stack"
        )


# ---------------------------------------------------------------------------
# Bundle sanity
# ---------------------------------------------------------------------------


def test_terminals_js_non_empty_and_capped():
    """A truncating edit usually halves the file. Cheap belt-and-braces."""
    src = _src()
    assert len(src) > 100_000, (
        f"terminals.js shrank to {len(src)} bytes — likely truncation"
    )
    assert src.lstrip().startswith("// .ai/dashboard/app/terminals.js"), (
        "header comment lost — likely destructive top-of-file edit"
    )
    assert "restoreOpenPanes" in src[-2000:], (
        "tail boot sequence lost — likely truncation"
    )
