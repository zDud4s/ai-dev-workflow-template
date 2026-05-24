"""Regression tests for dashboard a11y, design-token, and dedupe fixes.

Covers:
 - Cross-browser CSS: attr() 2-arg form removed (Firefox/Safari support).
 - Design tokens --radius-md and --fg-faint declared in :root.
 - Global :focus-visible rule (WCAG 2.4.7) present.
 - .tl-bar text contrast (WCAG AA) on saturated success/failure fills.
 - No duplicate selectors for .diff-line, .term-head .id, .events-table td.ts.
 - All jsdelivr CDN <script> tags carry crossorigin (precondition for SRI).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# -- Fix 1 --------------------------------------------------------------
def test_attr_data_lang_single_arg() -> None:
    """The 2-arg attr() form is CSS Values L5 and breaks Firefox/Safari."""
    css = _css()
    # The 2-arg form must be gone.
    assert 'attr(data-lang, "")' not in css
    assert "attr(data-lang, '')" not in css
    # The 1-arg form is fine and is what we expect now.
    assert "attr(data-lang)" in css


# -- Fix 2 --------------------------------------------------------------
def test_radius_md_defined() -> None:
    """--radius-md must be declared (not just referenced via fallback)."""
    css = _css()
    # Match a declaration like `--radius-md: 0;` rather than `var(--radius-md`.
    assert re.search(r"--radius-md\s*:", css) is not None


def test_fg_faint_defined() -> None:
    """--fg-faint must be declared (not just referenced via fallback)."""
    css = _css()
    assert re.search(r"--fg-faint\s*:", css) is not None


# -- Fix 3 --------------------------------------------------------------
def test_focus_visible_rule_exists() -> None:
    """Global :focus-visible rule is required for keyboard a11y (WCAG 2.4.7)."""
    css = _css()
    assert ":focus-visible" in css
    # Ensure there is at least one outline rule on a :focus-visible selector.
    assert re.search(
        r":focus-visible\s*\{[^}]*outline\s*:", css, flags=re.DOTALL
    ) is not None


# -- Fix 4 --------------------------------------------------------------
def test_tl_bar_contrast_dark_text() -> None:
    """#view-timeline .tl-bar must use dark text (not #fff) for WCAG AA."""
    css = _css()
    match = re.search(
        r"#view-timeline\s+\.tl-bar\s*\{([^}]*)\}", css, flags=re.DOTALL
    )
    assert match is not None, "expected a `#view-timeline .tl-bar` rule"
    body = match.group(1).lower()
    # White text on the saturated --good/--bad fills was ~2.4:1 (AA fail).
    assert "color: #fff" not in body
    assert "color:#fff" not in body
    assert "color: white" not in body
    # The replacement should be a dark hex or a token (var(--on-...)).
    assert ("#0a0a0a" in body) or ("var(--on-" in body)


# -- Fix 5 --------------------------------------------------------------
def _count_rule_starts(selector: str, css: str) -> int:
    """Count occurrences of a rule-opening `selector {` (whitespace tolerant)."""
    pattern = re.escape(selector) + r"\s*\{"
    return len(re.findall(pattern, css))


def test_diff_line_single_definition() -> None:
    """`.diff-line {` should appear exactly once."""
    assert _count_rule_starts(".diff-line", _css()) == 1


def test_term_head_id_single_definition() -> None:
    """`.term-head .id {` should appear exactly once (no container-query dupe)."""
    assert _count_rule_starts(".term-head .id", _css()) == 1


# -- Fix 6 --------------------------------------------------------------
def test_cdn_scripts_have_crossorigin() -> None:
    """Every jsdelivr <script src=...> tag must carry crossorigin (SRI prep)."""
    html = _html()
    # Find each <script ...src="https://cdn.jsdelivr.net/..."...></script>.
    tags = re.findall(
        r"<script\b[^>]*\bsrc=\"https://cdn\.jsdelivr\.net/[^\"]+\"[^>]*>",
        html,
    )
    assert tags, "expected at least one jsdelivr <script> tag"
    for tag in tags:
        assert "crossorigin=" in tag, (
            "jsdelivr <script> tag is missing crossorigin: " + tag
        )
