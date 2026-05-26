"""Regression tests for the proposals-modules batch of fixes.

Covers four targeted patches:
  1. skills.js — clears stale proposal cards when the visible list goes empty
     so a later restored `display = ""` cannot resurrect old content.
  2. agents.js — identical hide-vs-clear pattern for agent suggestions.
  3. skills.js openSkillDetail — guards the synchronous "loading…" path with
     a `_currentSkillKey` check too, not just post-await.
  4. auto-select.js — surfaces `err.message` instead of `String(err)` so
     well-formed Error objects don't render with the "Error: " prefix.

These tests are pure source-text assertions: they intentionally don't spin
up a browser. The previous batch's tests (test_skills_diff_modal.py,
test_agents_modal_race.py, test_dashboard_trivial_wins.py) already exercise
the modal Accept/Reject flow at a higher level.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def test_skills_clears_stale_content_on_hide():
    """Fix 1: the early-return branch in loadSkillProposals must wipe the
    wrap before hiding the block, so a later un-hide cannot leak stale
    proposal cards from a prior load."""
    src = _src("skills.js")
    # Locate the `if (!visible.length) {` block and ensure the body clears
    # the wrap before returning. We slice from the marker to the first
    # closing brace at the same indent level (heuristic: next `}` line).
    marker = "if (!visible.length)"
    idx = src.find(marker)
    assert idx != -1, "expected `if (!visible.length)` guard in skills.js"
    # Take a generous window after the marker to cover both single-line
    # and multi-line forms.
    window = src[idx:idx + 400]
    # Cut at the `return;` so we don't accidentally pick up later writes.
    cut = window.find("return;")
    assert cut != -1, "expected a `return;` inside the empty-visible guard"
    body = window[:cut]
    assert 'wrap.innerHTML = ""' in body or "wrap.innerHTML=''" in body or 'wrap.innerHTML = ' in body, (
        "skills.js empty-visible guard must clear wrap.innerHTML before returning; "
        "got:\n" + body
    )


def test_agents_clears_stale_content_on_hide():
    """Fix 2: same belt-and-braces clear in agents.js loadAgentProposals."""
    src = _src("agents.js")
    marker = "if (!visible.length)"
    idx = src.find(marker)
    assert idx != -1, "expected `if (!visible.length)` guard in agents.js"
    window = src[idx:idx + 400]
    cut = window.find("return;")
    assert cut != -1, "expected a `return;` inside the empty-visible guard"
    body = window[:cut]
    assert 'wrap.innerHTML = ""' in body or "wrap.innerHTML=''" in body or 'wrap.innerHTML = ' in body, (
        "agents.js empty-visible guard must clear wrap.innerHTML before returning; "
        "got:\n" + body
    )


def test_skills_openSkillDetail_guards_synchronous_too():
    """Fix 3: openSkillDetail should guard both the synchronous setup path
    and the post-await render path with `_currentSkillKey !==` checks so
    rapid click-spam can't leak a stale "loading…" or render into the
    wrong-skill modal."""
    src = _src("skills.js")
    # Extract just the openSkillDetail body. We bound it by the next
    # top-level `function` definition to avoid counting checks in other
    # functions that might mention _currentSkillKey.
    fn_start = src.find("async function openSkillDetail")
    assert fn_start != -1, "expected openSkillDetail function in skills.js"
    # The next `function ` declaration ends our window.
    next_fn = src.find("\n    function ", fn_start + 1)
    if next_fn == -1:
        next_fn = len(src)
    body = src[fn_start:next_fn]
    # Count the guard checks. The fix requires at least two: one before
    # the await (synchronous race guard) and one after (existing).
    guards = re.findall(r"_currentSkillKey\s*!==", body)
    assert len(guards) >= 2, (
        f"expected at least 2 `_currentSkillKey !==` guards in openSkillDetail; "
        f"found {len(guards)} — body excerpt:\n{body[:800]}"
    )


def test_auto_select_uses_err_message():
    """Fix 4: the error-rendering path must prefer `err.message` over
    `String(err)` so an `Error("HTTP 500")` renders as `HTTP 500` rather
    than `Error: HTTP 500`."""
    src = _src("auto-select.js")
    # The render path lives in the catch-block that writes "Failed to load:".
    idx = src.find("Failed to load:")
    assert idx != -1, "expected `Failed to load:` empty-state in auto-select.js"
    # Look at a window around the match. The fix lives on the same line
    # or one of the next few.
    window = src[idx:idx + 200]
    assert "err.message" in window or "e.message" in window, (
        "auto-select.js error renderer should use err.message (with a "
        "String(err) fallback); got:\n" + window
    )
    # Sanity: the un-fixed code was `escape(String(err))`. Make sure that
    # bare pattern is no longer the SOLE form on the failing-load line —
    # i.e. err.message must come before any String(err) fallback on that
    # same line.
    line = window.split("\n", 1)[0]
    if "String(err)" in line:
        assert line.find("err.message") < line.find("String(err)"), (
            "if String(err) remains as a fallback, err.message must come first "
            "in the same expression; got line:\n" + line
        )


def test_agent_modal_dismiss_handlers():
    """Agent proposal modal should dismiss via close button, backdrop, and Escape."""
    src = _src("core.js")
    assert "#agent-proposal-modal" in src, (
        "core.js should wire #agent-proposal-modal dismiss handling"
    )
    assert "closeAgentProposalModal" in src, (
        "core.js should call closeAgentProposalModal from agent proposal modal handlers"
    )

    idx = src.find('$("#agent-proposal-modal")')
    assert idx != -1, "expected #agent-proposal-modal lookup in core.js"
    window = src[max(0, idx - 600) : idx + 1200]
    assert "keydown" in window and "Escape" in window and "closeAgentProposalModal" in window, (
        "Escape modal handler should branch on #agent-proposal-modal and call "
        "closeAgentProposalModal"
    )


def test_suggest_agents_single_feedback():
    """Suggest agents should use the toast channel instead of inline msg writes."""
    src = _src("agents.js")
    fn_start = src.find("async function suggestAgents")
    assert fn_start != -1, "expected suggestAgents function in agents.js"
    next_fn = src.find("\n    async function ", fn_start + 1)
    if next_fn == -1:
        next_fn = src.find("\n    function ", fn_start + 1)
    if next_fn == -1:
        next_fn = len(src)
    body = src[fn_start:next_fn]

    assert not re.search(
        r'const\s+msg\s*=\s*\$\("#agent-suggest-msg"\)|msg\.textContent\s*=|\$\("#agent-suggest-msg"\)\.textContent\s*=',
        body,
    ), "suggestAgents should not write inline #agent-suggest-msg textContent"
    assert re.search(
        r'setMsg\(\s*"#agent-suggest-msg"\s*,\s*"ok"\s*,\s*`\$\{n\} new suggestion',
        body,
    ), "suggestAgents success branch should emit an ok toast"
    assert re.search(
        r'catch\s*\([^)]*\)\s*\{[^}]*setMsg\(\s*"#agent-suggest-msg"\s*,\s*"err"',
        body,
        flags=re.DOTALL,
    ), "suggestAgents failure branch should emit an error toast"
