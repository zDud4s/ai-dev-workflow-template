"""Round-2 dashboard polish guards.

Locks in the 5 fixes applied in this batch so they cannot silently regress:
  1. diff-line color uses theme tokens (--bad / --good), not raw hex.
  2. .term-pty-body background uses a token (not the literal #0b0f14).
  3. .agent-card h3 no longer carries the dead-code standard `line-clamp` prop
     (only the working `-webkit-line-clamp` remains).
  4. Inline onclick="loadAll()" / loadTimeline() / loadAutoSelect() in
     index.html are replaced by data-action attributes wired through a
     delegated click listener.
  5. (Spot-checked via the data-action listener test, plus the other guards.)
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _rule_block(selector: str, css: str) -> str:
    match = re.search(rf"(?m)^[ \t]*{re.escape(selector)}\s*\{{[^}}]*\}}", css)
    assert match, f"{selector} rule not found in styles.css"
    return match.group(0)


def test_header_notch_not_clipping():
    """The header corner notch is safe when it is reduced or explicitly
    layered below the action buttons; this batch applies both safeguards.
    """
    block = _rule_block("header::after", _css())
    assert ("width: 12px" in block) or ("z-index: 0" in block), (
        "header::after should either use the reduced notch or explicit layer"
    )


def _diff_line_block(css: str) -> str:
    """Return the chunk of CSS around the `.diff-line.removed` /
    `.diff-line.added` rules so we can assert about them specifically.
    """
    match = re.search(
        r"\.diff-line\.removed[^\n]*\n[^\n]*\.diff-line\.added[^\n]*",
        css,
    )
    assert match, ".diff-line.removed / .diff-line.added rules not found in styles.css"
    return match.group(0)


def _term_pty_body_block(css: str) -> str:
    """Return the `.term-pane.term-pty .term-pty-body { ... }` declaration block."""
    match = re.search(
        r"\.term-pane\.term-pty\s+\.term-pty-body\s*\{[^}]*\}",
        css,
    )
    assert match, ".term-pty-body rule not found in styles.css"
    return match.group(0)


def _agent_card_h3_block(css: str) -> str:
    """Return the `.agent-card h3 { ... }` declaration block."""
    match = re.search(r"\.agent-card\s+h3\s*\{[^}]*\}", css)
    assert match, ".agent-card h3 rule not found in styles.css"
    return match.group(0)


# ---------------------------------------------------------------------------
# Fix 1: diff-line uses theme tokens
# ---------------------------------------------------------------------------
def test_diff_line_uses_tokens():
    block = _diff_line_block(_css())
    # The two hardcoded hex values are gone …
    assert "#ff8888" not in block, "diff-line.removed still uses raw hex #ff8888"
    assert "#8ee29d" not in block, "diff-line.added still uses raw hex #8ee29d"
    # … and the tokens are now in place.
    assert "var(--bad)" in block, "diff-line.removed should use var(--bad)"
    assert "var(--good)" in block, "diff-line.added should use var(--good)"


# ---------------------------------------------------------------------------
# Fix 2: .term-pty-body uses a token
# ---------------------------------------------------------------------------
def test_term_pty_body_uses_token():
    block = _term_pty_body_block(_css())
    assert "#0b0f14" not in block, ".term-pty-body still uses raw hex #0b0f14"
    assert "var(--" in block, ".term-pty-body should use a CSS variable token"


# ---------------------------------------------------------------------------
# Fix 3: dead-code `line-clamp` removed from `.agent-card h3`
# ---------------------------------------------------------------------------
def test_line_clamp_dead_prop_removed():
    block = _agent_card_h3_block(_css())
    # The working webkit version must still be present.
    assert "-webkit-line-clamp" in block, "missing -webkit-line-clamp in .agent-card h3"
    # The bare `line-clamp:` standard prop must NOT be present (no browser
    # implements it; it was dead code). We look for `line-clamp:` not
    # preceded by `-webkit-`.
    assert not re.search(
        r"(?<!-webkit-)line-clamp\s*:",
        block,
    ), ".agent-card h3 still contains the bare standard `line-clamp` property"


# ---------------------------------------------------------------------------
# Fix 4a: inline onclick handlers replaced with data-action attributes
# ---------------------------------------------------------------------------
def test_no_inline_loadAll_onclick():
    html = _html()
    # No inline onclick="loadX(" remains for the three loader actions.
    assert 'onclick="loadAll(' not in html, "inline onclick='loadAll(' still present"
    assert 'onclick="loadTimeline(' not in html, "inline onclick='loadTimeline(' still present"
    assert 'onclick="loadAutoSelect(' not in html, "inline onclick='loadAutoSelect(' still present"
    # And the data-action attributes are now wired up instead.
    assert 'data-action="loadAll"' in html
    assert 'data-action="loadTimeline"' in html
    assert 'data-action="loadAutoSelect"' in html


# ---------------------------------------------------------------------------
# Fix 4b: delegated click listener for data-action is present
# ---------------------------------------------------------------------------
def test_delegated_action_listener_present():
    html = _html()
    # The delegated listener block lives at the end of <body> and looks up
    # `btn.dataset.action` on click. We assert on a couple of stable
    # substrings so harmless reformatting won't break the test.
    assert 'data-action' in html
    assert 'addEventListener("click"' in html or "addEventListener('click'" in html, (
        "expected a delegated click listener for [data-action] in index.html"
    )
    assert "dataset.action" in html, (
        "delegated listener should read btn.dataset.action"
    )
