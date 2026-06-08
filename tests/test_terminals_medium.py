"""Static-lint tests for the 2026-05-23 terminals.js MEDIUM/LOW fixes.

The dashboard has no jsdom harness, so we assert on source-level invariants
that prove the fixes are present. Pattern modeled on
``tests/test_terminals_fixes.py`` and ``tests/test_terminals_remaining_high.py``.

Covers four fixes:
  1. Three remaining ad-hoc pill-class manipulations now route through the
     existing ``termSetPillState`` helper so stale state classes cannot
     accumulate across transitions.
  2. Five high-traffic empty ``catch (_) {}`` blocks were upgraded to log
     via ``console.warn("[terminals] ...")`` so silent failures become
     greppable in the browser console.
  3. ``termCloseAllFinished`` snapshots ``TERMS.keys()`` before iterating so
     a cascading ``termClose`` (chat-codex rekey, transcript companion)
     cannot skip a sibling entry mid-mutation.
  4. The chat resume-from-dead path now propagates the operator's chosen
     model when POSTing /api/jobs, matching the codex resume path.
"""

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
# Fix 1 — pill class stacking normalization (3 sites)
# ---------------------------------------------------------------------------

def test_pill_state_helper_used_in_codex_paths():
    """All three identified pill mutation sites now route through the helper.

    Sites:
      * ``termCodexAwaitNextTurn`` (was: classList.remove + add("done"))
      * ``termSendCodexNextTurn`` connecting branch (was: remove + add("running"))
      * dispatch-tracker result block (was: classList.toggle("done", !isError))
    """
    src = _src()

    await_body = _slice_function(src, "async function termCodexAwaitNextTurn(")
    assert "termSetPillState(" in await_body, (
        "termCodexAwaitNextTurn must use termSetPillState to clear stale "
        "running/queued/cancelling classes when going to ready/done"
    )
    # The literal pattern the helper replaced must be gone from this function.
    assert 'sp.classList.add("done")' not in await_body, (
        "termCodexAwaitNextTurn still mutates the pill via classList.add — "
        "must route through termSetPillState"
    )

    send_body = _slice_function(src, "async function termSendCodexNextTurn(")
    assert "termSetPillState(" in send_body, (
        "termSendCodexNextTurn's connecting branch must use termSetPillState "
        "to normalize the pill before adding 'running'"
    )
    assert 'sp.classList.add("running")' not in send_body, (
        "termSendCodexNextTurn still mutates the pill via classList.add — "
        "must route through termSetPillState"
    )

    # Dispatch-tracker result rendering — the third site. The old line was
    # ``status.classList.toggle("done", !isError)``; it must be gone.
    assert "classList.toggle(\"done\"" not in src, (
        "the dispatch result block still uses classList.toggle('done', ...) "
        "instead of termSetPillState — stale running/queued classes survive"
    )


# ---------------------------------------------------------------------------
# Fix 2 — empty catch blocks logging
# ---------------------------------------------------------------------------

def test_no_empty_catch_in_terminals():
    """High-traffic dashboard catch blocks must log via console.warn.

    We don't require *every* catch to log (per-frame xterm focus / scrollIntoView
    catches would spam the console). The contract is that the high-value catches
    still owned by terminals.js surface a "[terminals] ..." warn so silent
    failures become greppable. PTY WebSocket rendering moved to pane-core.js.
    """
    src = _src()
    warn_in_catch = re.findall(
        r'catch \(e\) \{ console\.warn\("\[terminals\][^"]+"',
        src,
    )
    assert len(warn_in_catch) >= 3, (
        f"expected >=3 console.warn-bearing catch blocks for high-traffic "
        f"sites; found {len(warn_in_catch)}"
    )

    # Spot-check specific high-value sites by their context phrase.
    assert "[terminals] termClose: SSE close failed" in src, (
        "termClose's SSE close catch must log so leaked EventSource closes "
        "are diagnosable"
    )
    assert "[terminals] codex function_call args parse failed" in src, (
        "codex function_call args JSON.parse catch must log so malformed "
        "tool calls are diagnosable"
    )
    assert "function termOpenPty" not in src


# ---------------------------------------------------------------------------
# Fix 3 — termCloseAllFinished snapshots keys
# ---------------------------------------------------------------------------

def test_termCloseAllFinished_snapshots_keys():
    """The Map must be snapshotted before iteration to survive cascading closes."""
    src = _src()
    body = _slice_function(src, "function termCloseAllFinished(")
    # Accept either the spread or Array.from form.
    snapshotted = (
        "[...TERMS.keys()]" in body
        or "Array.from(TERMS.keys())" in body
    )
    assert snapshotted, (
        "termCloseAllFinished must iterate over a snapshot of TERMS.keys() "
        "so a cascading termClose doesn't skip a sibling entry mid-iteration"
    )
    # The unsafe live-iteration pattern must be gone.
    assert "for (const [jobId, t] of TERMS.entries())" not in body, (
        "termCloseAllFinished still iterates TERMS.entries() live — replace "
        "with a [...TERMS.keys()] snapshot"
    )


# ---------------------------------------------------------------------------
# Fix 4 — chat resume includes model field
# ---------------------------------------------------------------------------
# Removed: the dead-chat resume path (termSendResumeChat) was deleted when
# Claude chats converged on the unified session pane.
