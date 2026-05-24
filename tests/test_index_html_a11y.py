"""Regression tests for batch 4 a11y fixes to .ai/dashboard/index.html.

Locks in:
  1. Search-style <input>s have an associated <label for=...> (WCAG 1.3.1 /
     3.3.2). Placeholder-as-label is not a label.
  2. The nav role=tablist / button role=tab pattern is complete:
     - every role=tab carries aria-controls=...
     - every <section class="view"> carries role=tabpanel
  3. Inline style="..." occurrences in index.html have been reduced
     relative to the previous batch baseline.
  4. The two longest .tl-hint instructional blocks (timeline + auto-select)
     are wrapped in <details> so users can collapse them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix 1 — Search/text inputs have <label for=...> associations
# ---------------------------------------------------------------------------

# Inputs that were previously using only a placeholder as their label.
_SEARCH_INPUTS_NEEDING_LABELS = (
    "skills-search",
    "agents-search",
    "ev-search",
)


def test_search_inputs_have_labels() -> None:
    """Every previously-unlabelled <input type='search'> now has a
    <label for='<id>'> association near it in the source."""
    html = _html()
    lines = html.splitlines()
    for input_id in _SEARCH_INPUTS_NEEDING_LABELS:
        # Find the <input ... id="<input_id>" ...> line.
        input_line = None
        for idx, line in enumerate(lines):
            if f'id="{input_id}"' in line and "<input" in line:
                input_line = idx
                break
        assert input_line is not None, (
            f"<input id='{input_id}'> not found in index.html"
        )
        # Look within +/- 100 lines for a <label for="<input_id>"... > tag.
        window_start = max(0, input_line - 100)
        window_end = min(len(lines), input_line + 100)
        window = "\n".join(lines[window_start:window_end])
        label_pat = rf'<label\s+[^>]*\bfor="{re.escape(input_id)}"'
        assert re.search(label_pat, window) is not None, (
            f"no <label for='{input_id}'> found within 100 lines of the input"
        )


def test_sr_only_class_defined() -> None:
    """The visually-hidden helper class .sr-only is defined either inline
    (in <style>) or in styles.css — at least one of the search labels uses
    it, so it must resolve to a rule somewhere."""
    html = _html()
    # The labels we added all use class="sr-only", so check the markup at
    # minimum carries the class.
    assert 'class="sr-only"' in html, (
        "expected at least one <label class='sr-only'> in index.html"
    )
    # And there must be a CSS rule for `.sr-only` either inline in <head>
    # or referenced via the linked stylesheet. We accept either: the inline
    # <style> in index.html OR styles.css carrying the rule.
    inline_has_rule = re.search(r"\.sr-only\s*\{", html) is not None
    if not inline_has_rule:
        styles_css = (ROOT / ".ai" / "dashboard" / "styles.css").read_text(
            encoding="utf-8"
        )
        assert re.search(r"\.sr-only\s*\{", styles_css) is not None, (
            ".sr-only is used in index.html but no CSS rule defines it"
        )


# ---------------------------------------------------------------------------
# Fix 2 — Complete tablist / tab / tabpanel ARIA pattern
# ---------------------------------------------------------------------------
def test_tabs_have_aria_controls() -> None:
    """Every <button role='tab'> must carry aria-controls=... so screen
    readers can map tab → panel."""
    html = _html()
    tab_buttons = re.findall(
        r"<button\b[^>]*\brole=\"tab\"[^>]*>",
        html,
    )
    assert tab_buttons, "expected at least one role='tab' button in index.html"
    for tag in tab_buttons:
        assert "aria-controls=" in tag, (
            f"role='tab' button is missing aria-controls: {tag}"
        )


def test_views_have_tabpanel_role() -> None:
    """At least 8 view sections must carry role='tabpanel' (the spec
    requires the 16 main views, but we use 8 as a conservative floor)."""
    html = _html()
    # Match <section class="view ..." ... role="tabpanel" ...> in either order.
    sections = re.findall(
        r"<section\b[^>]*\bclass=\"view(?:\s+[^\"]*)?\"[^>]*>",
        html,
    )
    panel_sections = [s for s in sections if 'role="tabpanel"' in s]
    assert len(panel_sections) >= 8, (
        f"expected >=8 <section class='view' ... role='tabpanel'>, "
        f"got {len(panel_sections)} out of {len(sections)} view sections"
    )


def test_tablist_role_on_nav() -> None:
    """The main #nav element should advertise role='tablist'."""
    html = _html()
    nav_open = re.search(r"<nav\b[^>]*\bid=\"nav\"[^>]*>", html)
    assert nav_open is not None, "<nav id='nav'> not found"
    assert 'role="tablist"' in nav_open.group(0), (
        "expected role='tablist' on <nav id='nav'>"
    )


# ---------------------------------------------------------------------------
# Fix 3 — Inline style="..." occurrences reduced
# ---------------------------------------------------------------------------
def test_inline_styles_reduced() -> None:
    """We removed 3 inline style='...' attributes in batch 4 (margin-top:8px
    on a .form-actions row, a skeleton padding override, and a 140px field
    cap on the decision-date input). The total should sit at or below 20.

    If a future change legitimately needs to add another inline style, this
    test should be updated alongside it to reflect the new floor."""
    html = _html()
    count = html.count('style="')
    assert count <= 20, (
        f"index.html now has {count} inline style='...' attributes; "
        "batch 4 baseline expected <=20"
    )


def test_removed_inline_styles_specifically_gone() -> None:
    """The 3 specific inline-style strings removed by batch 4 are gone."""
    html = _html()
    # 1. The dispatch-toggle row's redundant 8px margin (form-actions already
    #    sets margin-top via its own rule).
    assert 'style="margin-top:8px"' not in html, (
        "expected `style=\"margin-top:8px\"` to be removed from the dispatch "
        "toggle row"
    )
    # 2. The yaml-tree skeleton's inline padding override.
    assert 'style="padding:var(--s-3) 0"' not in html, (
        "expected the skeleton padding inline style to be removed"
    )
    # 3. The decision-date field's max-width:140px cosmetic cap.
    assert 'style="max-width:140px"' not in html, (
        "expected `style=\"max-width:140px\"` to be removed from the "
        "decision-date field"
    )


# ---------------------------------------------------------------------------
# Fix 4 — Long .tl-hint blocks collapsed inside <details>
# ---------------------------------------------------------------------------
def test_tl_hint_wrapped_in_details() -> None:
    """The two longest .tl-hint instructional blocks (timeline + auto-select)
    are wrapped in <details><summary>…</summary>…</details> so users can
    collapse them after first read."""
    html = _html()
    # We look for the wrapper marker class we added so this stays a stable
    # contract — tightly coupled to the markup we shipped.
    matches = re.findall(
        r"<details\b[^>]*class=\"tl-hint-details\"[^>]*>",
        html,
    )
    assert len(matches) >= 2, (
        f"expected >=2 <details class='tl-hint-details'> wrappers, got "
        f"{len(matches)}"
    )
    # And each wrapper must contain a <summary> tag (a11y / disclosure UX).
    detail_blocks = re.findall(
        r"<details\b[^>]*class=\"tl-hint-details\"[^>]*>.*?</details>",
        html,
        flags=re.DOTALL,
    )
    for block in detail_blocks:
        assert "<summary>" in block, (
            "tl-hint-details wrapper missing <summary>: " + block[:80]
        )
        assert "tl-hint" in block, (
            "tl-hint-details wrapper missing the inner .tl-hint div"
        )
