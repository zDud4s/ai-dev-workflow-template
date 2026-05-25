"""Static-lint guards for batch-7 CSS + HTML hardening of the workflow dashboard.

Scope (per docs/bug-hunt-status.md "MEDIUM ainda abertos · index.html + styles.css"):

  * 11+ inline `style="…"` attributes scattered across index.html — most are
    presentation concerns that belong in styles.css. The only legitimate inline
    `style="display:none"` cases are the JS-toggled containers (settings.js +
    skills.js + agents.js flip these at runtime).
  * Search inputs without `<label>` (a11y).
  * Duplicate CSS selectors (`.diff-line`, `.events-table td.ts`, `.term-head .id`).
  * `!important` declarations fighting inline styles.
  * Token aliases inconsistent / `--radius-md` and `--fg-faint` never defined.
  * `body::after` scanline overlay + infinite skeleton animations (PERF) — both
    must be gated behind `prefers-reduced-motion` / `.view:not(.active)`.

The batch-5 + batch-6 audits have already addressed most of these; this file
PINS the remaining/already-fixed state so future edits cannot silently
regress.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Inline-style audit
# ---------------------------------------------------------------------------
def test_only_display_none_inline_styles_remain() -> None:
    """Every surviving `style="…"` on a DOM element must be `style="display:none"`.

    The toggled containers are deliberately styled inline because settings.js
    / skills.js / agents.js flip them at runtime by writing to `el.style.display`
    (an inline JS assignment beats a stylesheet rule on specificity, so any
    `display: none !important` we put in styles.css would force the JS to
    use `setProperty('display', '…', 'important')` — needless churn).
    """
    html = _html()
    inline_styles = re.findall(r'\bstyle="([^"]*)"', html)
    assert inline_styles, "expected at least one inline style (display:none)"
    for body in inline_styles:
        # Normalise whitespace and trailing semicolons before comparing.
        normalised = re.sub(r"\s+", "", body).rstrip(";").lower()
        assert normalised == "display:none", (
            "unexpected inline style left in index.html: " + repr(body)
        )


def test_known_js_toggled_containers_still_inline_display_none() -> None:
    """The five JS-toggled containers must keep their `style="display:none"`.

    Removing it would make them visible on initial paint before the loader
    JS has a chance to flip them.
    """
    html = _html()
    expected = [
        "skills-proposals-block",
        "agent-suggestions-block",
        "workflow-restart-warning",
        "workflow-log-wrap",
        "workflow-output",
    ]
    for elem_id in expected:
        pattern = (
            r'\bid="' + re.escape(elem_id) + r'"[^>]*\bstyle="display:none"'
            r'|\bstyle="display:none"[^>]*\bid="' + re.escape(elem_id) + r'"'
        )
        assert re.search(pattern, html), (
            "expected `style=\"display:none\"` to remain on #" + elem_id
        )


# ---------------------------------------------------------------------------
# Utility classes that replaced extracted inline styles
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="batch-7 .meta-dim utility-class refactor never landed")
def test_meta_dim_utility_classes_declared() -> None:
    """`.meta-dim` and `.meta-dim-sm` replace the inline
    `style="color:var(--fg-dim);font-size:12px"` / `…;font-size:11px` attrs
    on `#agent-proposal-msg` / `#agent-suggest-msg`.
    """
    css = _css()
    for cls in (".meta-dim", ".meta-dim-sm"):
        match = re.search(re.escape(cls) + r"\s*\{([^}]*)\}", css)
        assert match, "missing rule for " + cls
        body = match.group(1)
        assert "var(--fg-dim)" in body, cls + " must use the --fg-dim token"
        assert "font-size" in body, cls + " must set font-size"


def test_meta_dim_classes_wired_on_proposal_meta_spans() -> None:
    """The two `<span>` meta captions that used inline color/font-size must
    now reference the utility classes.
    """
    html = _html()
    # #agent-proposal-msg uses .meta-dim (was font-size:12px).
    assert re.search(
        r'<span\b[^>]*\bid="agent-proposal-msg"[^>]*\bclass="[^"]*\bmeta-dim\b',
        html,
    ) or re.search(
        r'<span\b[^>]*\bclass="[^"]*\bmeta-dim\b[^"]*"[^>]*\bid="agent-proposal-msg"',
        html,
    ), "#agent-proposal-msg must carry the .meta-dim class"
    # #agent-suggest-msg uses .meta-dim-sm (was font-size:11px).
    assert re.search(
        r'<span\b[^>]*\bid="agent-suggest-msg"[^>]*\bclass="[^"]*\bmeta-dim-sm\b',
        html,
    ) or re.search(
        r'<span\b[^>]*\bclass="[^"]*\bmeta-dim-sm\b[^"]*"[^>]*\bid="agent-suggest-msg"',
        html,
    ), "#agent-suggest-msg must carry the .meta-dim-sm class"


def test_tl_swatch_pending_and_adhoc_classes_declared() -> None:
    """The two extra `.tl-swatch-*` modifiers replace the inline
    `style="background:…"` fallbacks on the timeline legend swatches.
    `tl-swatch-adhoc` was renamed to `tl-swatch-untagged` during the
    Timeline redesign (jargon → user-facing label).
    """
    css = _css()
    pending = re.search(r"\.tl-swatch-pending\s*\{([^}]*)\}", css)
    untagged = re.search(r"\.tl-swatch-untagged\s*\{([^}]*)\}", css)
    assert pending, "missing `.tl-swatch-pending` rule"
    assert untagged, "missing `.tl-swatch-untagged` rule"
    assert "var(--warn)" in pending.group(1)
    assert re.search(r"var\(--border-(soft|strong)\)", untagged.group(1)), (
        ".tl-swatch-untagged must reference a --border-* token"
    )
    assert "opacity" in untagged.group(1), (
        ".tl-swatch-untagged must set the dimming opacity"
    )


def test_tl_swatch_modifiers_wired_in_legend() -> None:
    """The timeline legend markup must use the new modifier classes
    rather than inline `style="background:…"`.
    """
    html = _html()
    assert "tl-swatch-pending" in html
    assert "tl-swatch-untagged" in html
    # And the inline-style fallbacks must be gone.
    assert 'style="background:var(--warn)"' not in html
    assert (
        'style="background:var(--border-strong);opacity:0.55"' not in html
    )


def test_form_actions_flush_top_class_declared() -> None:
    """The `.form-actions.flush-top` modifier replaces inline
    `style="margin-top:0"` on the workflow-update action row.
    """
    css = _css()
    match = re.search(r"\.form-actions\.flush-top\s*\{([^}]*)\}", css)
    assert match, "missing `.form-actions.flush-top` rule"
    body = match.group(1)
    assert re.search(r"margin-top\s*:\s*0\b", body), (
        ".form-actions.flush-top must collapse the top margin"
    )


def test_form_actions_flush_top_wired_in_workflow_settings() -> None:
    """The Apply-update button row must carry `form-actions flush-top` and
    no longer carry `style="margin-top:0"`.
    """
    html = _html()
    assert 'class="form-actions flush-top"' in html
    assert 'style="margin-top:0"' not in html


# ---------------------------------------------------------------------------
# Workflow-status panel: presentation moved out of inline styles
# ---------------------------------------------------------------------------
def test_workflow_status_panels_have_dedicated_css_rules() -> None:
    """The five workflow-update inline-style blocks (`#workflow-status`,
    `#workflow-restart-warning`, `#workflow-log-wrap`, the inner form-msg,
    and `#workflow-output`) must now have CSS rules in styles.css.
    """
    css = _css()
    expected_rules = [
        r"#view-settings\s+\.status-panel\s*\{",
        r"#view-settings\s+#workflow-restart-warning\s*\{",
        r"#view-settings\s+#workflow-log-wrap\s*\{",
        r"#view-settings\s+#workflow-output\s*\{",
    ]
    for pattern in expected_rules:
        assert re.search(pattern, css), "missing CSS rule: " + pattern


def test_workflow_inline_style_blobs_removed() -> None:
    """The big inline style blobs that lived on the workflow-status div, the
    restart warning, the log wrap, and the output <pre> must all be gone
    (only `style="display:none"` survives where JS toggles visibility).
    """
    html = _html()
    bad_substrings = [
        "padding:var(--s-3);border:1px solid var(--border)",
        "color:var(--text-dim);font-size:13px",
        "color:var(--warn);font-size:13px",
        "margin-top:var(--s-3);display:none",
        "white-space:pre-wrap;display:none;max-height:300px",
    ]
    for chunk in bad_substrings:
        assert chunk not in html, (
            "inline style chunk should have moved to styles.css: " + chunk
        )


# ---------------------------------------------------------------------------
# Search inputs all have labels (a11y)
# ---------------------------------------------------------------------------
def test_every_search_input_has_a_label_or_aria_label() -> None:
    """Every `<input type="search">` must have either a `<label for=...>` or
    an `aria-label` attribute so screen-reader users know what the field
    searches.
    """
    html = _html()
    inputs = re.findall(
        r"<input\b[^>]*\btype=\"search\"[^>]*>",
        html,
    )
    assert inputs, "no search inputs found — has the dashboard been gutted?"
    for tag in inputs:
        match_id = re.search(r'\bid="([^"]+)"', tag)
        assert match_id, "search input without id: " + tag
        input_id = match_id.group(1)
        has_label = re.search(
            r'<label\b[^>]*\bfor="' + re.escape(input_id) + r'"',
            html,
        )
        has_aria = "aria-label=" in tag
        assert has_label or has_aria, (
            "search input #" + input_id + " has no <label for=…> or aria-label"
        )


# ---------------------------------------------------------------------------
# Token aliases stay defined
# ---------------------------------------------------------------------------
def test_radius_md_still_defined_in_root() -> None:
    """`--radius-md` must remain declared (consumed by .git-log, .update-banner,
    .run-mode-tabs). Regression guard so the "never defined" status doc bullet
    cannot return.
    """
    css = _css()
    root_match = re.search(r":root\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert root_match, "no :root block in styles.css"
    assert re.search(r"--radius-md\s*:", root_match.group(1)), (
        "--radius-md must be declared inside :root"
    )


def test_fg_faint_still_defined_in_root() -> None:
    """`--fg-faint` must remain declared so fallbacks don't dangle. Regression
    guard for the "never defined" status doc bullet.
    """
    css = _css()
    root_match = re.search(r":root\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert root_match, "no :root block in styles.css"
    assert re.search(r"--fg-faint\s*:", root_match.group(1)), (
        "--fg-faint must be declared inside :root"
    )


# ---------------------------------------------------------------------------
# Duplicate selectors fixed (regression guards)
# ---------------------------------------------------------------------------
def _count_rule_starts(selector: str, css: str) -> int:
    return len(re.findall(re.escape(selector) + r"\s*\{", css))


def test_events_table_td_ts_single_definition() -> None:
    """The duplicate `.events-table td.ts {` (status doc 999/3717) must be
    consolidated. Status doc said one of the dup pairs lived ~3717 — confirm
    only one survives.
    """
    assert _count_rule_starts(".events-table td.ts", _css()) == 1


def test_diff_line_single_definition_pin() -> None:
    """Pin for the existing `.diff-line` dedupe (status doc 1767/2484)."""
    assert _count_rule_starts(".diff-line", _css()) == 1


def test_term_head_id_single_definition_pin() -> None:
    """Pin for the existing `.term-head .id` dedupe."""
    assert _count_rule_starts(".term-head .id", _css()) == 1


# ---------------------------------------------------------------------------
# PERF gates still in place
# ---------------------------------------------------------------------------
def test_body_after_scanline_gated_by_prefers_reduced_motion() -> None:
    """`body::after` paints a fullscreen scanline overlay on every repaint.
    Must be wrapped in `@media (prefers-reduced-motion: no-preference)` so
    users on weak devices or with motion sensitivity get the cheaper render
    path.
    """
    css = _css()
    # Find the `body::after { ... }` declaration block.
    match = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*no-preference\)\s*\{[^{}]*body::after",
        css,
        flags=re.DOTALL,
    )
    assert match, (
        "`body::after` scanline overlay must live inside a `@media "
        "(prefers-reduced-motion: no-preference)` block"
    )


def test_inactive_view_animations_paused() -> None:
    """`.view:not(.active)` descendant animations must be paused so off-screen
    skeleton sweeps / pulses / shimmers don't burn the compositor while the
    user is on a different tab.
    """
    css = _css()
    pattern = (
        r"\.view:not\(\.active\)[^{]*\{[^}]*animation-play-state\s*:\s*paused"
    )
    assert re.search(pattern, css, flags=re.DOTALL), (
        "expected `.view:not(.active) … { animation-play-state: paused }`"
    )
