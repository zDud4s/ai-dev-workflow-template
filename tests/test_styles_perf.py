"""Performance-focused regression tests for the dashboard stylesheet.

These guard against regressions on four perf fixes:

1. The fullscreen scanline overlay (body::after) is gated behind
   `prefers-reduced-motion: no-preference` so that users who opt INTO reduced
   motion don't pay for a fullscreen compositor layer on every scroll.
2. Animations on elements inside inactive views (`.view:not(.active)`) are
   paused, not removed, so they resume cleanly when the view becomes active
   again while saving CPU on weak devices.
3. The `.card::before` rule no longer duplicates the parent's `clip-path`,
   removing 8+ redundant clipping operations on first paint (one per card).
4. `transition: all` (a perf smell — browsers must animate every property
   change) is avoided in favour of explicit property lists.
"""

import re
from pathlib import Path

STYLES_CSS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "styles.css"


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _rule_block(selector: str, css: str) -> str:
    match = re.search(rf"(?m)^[ \t]*{re.escape(selector)}\s*\{{[^}}]*\}}", css)
    assert match, f"{selector} rule not found in styles.css"
    return match.group(0)


def _keyframes_body(css: str, name: str) -> str:
    match = re.search(r"@keyframes\s+" + re.escape(name) + r"\s*\{", css)
    assert match, "Could not find @keyframes " + name
    start = match.end() - 1
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : i]
    raise AssertionError("Unbalanced braces in @keyframes " + name)


def test_keyframes_avoid_box_shadow():
    """Pulse animations must avoid animating box-shadow, which is paint-heavy."""
    css = _css()
    for name in ("pulse-dot", "term-needs-action"):
        body = _keyframes_body(css, name)
        assert "box-shadow:" not in body, (
            "@keyframes " + name + " must animate opacity/transform only"
        )


def test_body_after_gated_by_reduced_motion():
    """body::after must live inside a @media (prefers-reduced-motion: no-preference) block."""
    css = _css()
    # Match a no-preference media block (allowing whitespace variations) and capture
    # everything up to its closing `}` so we can check it contains `body::after`.
    pattern = re.compile(
        r"@media\s*\(\s*prefers-reduced-motion\s*:\s*no-preference\s*\)\s*\{",
        re.IGNORECASE,
    )
    match = pattern.search(css)
    assert match, "Expected a @media (prefers-reduced-motion: no-preference) block"

    # Walk braces from the opening `{` to find the matching close.
    start = match.end() - 1  # position of the `{`
    depth = 0
    end = None
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, "Unbalanced braces in prefers-reduced-motion media block"

    block_body = css[start:end]
    assert "body::after" in block_body, (
        "body::after scanline overlay must be inside the "
        "@media (prefers-reduced-motion: no-preference) block"
    )


def test_inactive_view_animations_paused():
    """Inactive views must pause animations to save CPU on weak devices."""
    css = _css()
    # Look for a selector targeting .view:not(.active) descendants with an
    # animation-play-state: paused declaration. We match the selector and the
    # rule body within a reasonable window.
    pattern = re.compile(
        r"\.view:not\(\.active\)[^\{]*\{[^\}]*animation-play-state\s*:\s*paused",
        re.IGNORECASE | re.DOTALL,
    )
    assert pattern.search(css), (
        "Expected a rule pausing animations under .view:not(.active) "
        "(animation-play-state: paused)"
    )


def test_card_before_has_no_clip_path():
    """`.card::before` must NOT redeclare clip-path (parent .card already clips it)."""
    css = _css()
    # Find the .card::before block and inspect its body. We anchor on the literal
    # selector at the start of a line to avoid catching .card-foo::before etc.
    match = re.search(
        r"^\s*\.card::before\s*\{([^}]*)\}",
        css,
        re.MULTILINE,
    )
    assert match, "Could not find .card::before rule"
    body = match.group(1)
    # Strip CSS comments before checking — explanatory comments may mention the
    # word `clip-path` legitimately; we only care about active declarations.
    body_no_comments = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    assert "clip-path" not in body_no_comments, (
        ".card::before should not redeclare clip-path; the parent .card already "
        "clips this absolutely-positioned pseudo, so the duplicate is wasted GPU work"
    )


def test_no_transition_all_in_hot_selectors():
    """`transition: all` is a perf smell; keep its count low (<5)."""
    css = _css()
    # Count both `transition: all` and `transition:all` (allow any whitespace).
    occurrences = re.findall(r"transition\s*:\s*all\b", css, re.IGNORECASE)
    assert len(occurrences) < 5, (
        f"Found {len(occurrences)} uses of `transition: all`; replace with explicit "
        "property lists to avoid animating every property change"
    )


def test_list_item_hover_no_padding_transition():
    """List-item hover should animate transform instead of reflowing padding."""
    css = _css()
    item = _rule_block(".list-item", css)
    hover = _rule_block(".list-item:hover", css)
    transition = re.search(r"transition\s*:\s*([^;]+);", item)
    assert transition, "expected .list-item to declare an explicit transition"
    assert "transform" in transition.group(1)
    assert "padding-left" not in transition.group(1)
    assert re.search(r"transform\s*:\s*(?:none|translateX\(0\))\s*;", item)
    assert re.search(r"transform\s*:\s*translateX\(2px\)\s*;", hover)
    assert "padding-left:" not in hover
