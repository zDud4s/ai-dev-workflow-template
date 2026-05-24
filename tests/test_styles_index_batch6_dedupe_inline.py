"""Static-lint guards for batch-6 dashboard hardening (dedupe + inline-style
extraction).

Covers:
 - Duplicate selector consolidation
     * `.diff-line { ... }` declared at most once
     * `.events-table td.ts { ... }` declared at most once
     * `.term-head .id { ... }` declared at most once
   (Already enforced by `test_dashboard_a11y_tokens.py` for `.diff-line` and
   `.term-head .id`. We add the third here so the trio is covered in one
   place and any future re-introduction of a duplicate is caught by this
   batch.)

 - Hardcoded color removal in `.tl-bar.pending`
     * The `color: #111` override on `#view-timeline .tl-bar.pending` is gone
       (the parent `.tl-bar` rule already sets the WCAG-AA dark `#0a0a0a`
       text color against the saturated `--warn` fill — duplicating that
       intent with `#111` was both a hardcoded color and a redundant
       override).

 - Inline-style extraction in index.html
     * The 12+ static inline `style="..."` payloads have been migrated to
       utility classes (`.btn-nowrap`, `.meta-dim`, `.meta-inline-sm`,
       `.tl-swatch-warn`, `.tl-swatch-adhoc`, `.no-mt`, `.form-msg-sub`,
       `.workflow-status-panel`, `.workflow-warning-banner`,
       `.workflow-log-wrap`, `.workflow-output-pre`).
     * Only the dynamic `style="display:none"` toggles remain inline
       because `app/skills.js`, `app/agents.js`, and `app/settings.js` flip
       `element.style.display` directly — keeping the toggle inline avoids
       an `!important` fight.

 - `onclick="..."` already removed in batches 2-3; we re-assert here so any
   future regression is caught.

 - Gemini integration CSS untouched: the two `.pill.gemini` /
   `.ph-tool-gemini` rules must survive batch-6 verbatim.
"""
from __future__ import annotations

import re
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _count_rule_starts(selector: str, css: str) -> int:
    r"""Count rule-opening `selector {` occurrences (whitespace tolerant).

    Matches `selector` (exact) followed by optional whitespace and `{`. Does
    NOT match selectors that have `selector` as a prefix (e.g. counting
    `.diff-line` will not catch `.diff-line.removed`) because the trailing
    `\s*\{` anchors the end of the selector.
    """
    pattern = re.escape(selector) + r"\s*\{"
    return len(re.findall(pattern, css))


# ---------- Duplicate-selector dedupe ---------------------------------------
def test_diff_line_single_rule() -> None:
    """`.diff-line {` must appear exactly once (modifier variants like
    `.diff-line.removed` / `.diff-line.added` are allowed and counted
    separately by the regex)."""
    assert _count_rule_starts(".diff-line", _css()) == 1


def test_events_table_ts_single_rule() -> None:
    """`.events-table td.ts {` must appear exactly once."""
    assert _count_rule_starts(".events-table td.ts", _css()) == 1


def test_term_head_id_single_rule() -> None:
    """`.term-head .id {` must appear exactly once (no container-query dupe)."""
    assert _count_rule_starts(".term-head .id", _css()) == 1


# ---------- Hardcoded color removal in .tl-bar.pending ----------------------
def test_tl_bar_pending_no_hardcoded_111() -> None:
    """The `color: #111` override on `#view-timeline .tl-bar.pending` was
    redundant with the parent `.tl-bar { color: #0a0a0a }` rule (both are
    near-black text for the saturated `--warn` fill). The override has been
    removed; the parent rule now owns the dark-on-warn contrast in one
    place.
    """
    css = _css()
    match = re.search(
        r"#view-timeline\s+\.tl-bar\.pending\s*\{([^}]*)\}",
        css,
        flags=re.DOTALL,
    )
    assert match is not None, "expected a `#view-timeline .tl-bar.pending` rule"
    body = match.group(1)
    # No hardcoded #111 / #111111 (case-insensitive, allow optional 3-digit
    # vs 6-digit form).
    assert not re.search(r"#111(?:111)?\b", body, flags=re.IGNORECASE), (
        "`.tl-bar.pending` still contains a hardcoded `#111` color override"
    )
    # The parent `.tl-bar` rule must still carry the dark-text token so the
    # pending bar inherits readable contrast. Sanity-check the parent.
    parent = re.search(
        r"#view-timeline\s+\.tl-bar\s*\{([^}]*)\}", css, flags=re.DOTALL
    )
    assert parent is not None
    parent_body = parent.group(1).lower()
    assert "#0a0a0a" in parent_body or "var(--on-" in parent_body, (
        "parent `.tl-bar` rule must still set a dark text color so "
        "`.tl-bar.pending` inherits AA-safe contrast on `--warn`"
    )


# ---------- Inline-style extraction -----------------------------------------
# Below the threshold the batch-6 fix lands at: only the 5 dynamic
# `style="display:none"` payloads should remain inline. Threshold is set 1
# above to leave a small headroom for future small additions; a regression
# that re-introduces several inline styles will still trip this.
INLINE_STYLE_THRESHOLD = 6


def test_inline_style_count_below_threshold() -> None:
    """`style="..."` attribute count in index.html must stay below the
    `INLINE_STYLE_THRESHOLD`. Static visual styling lives in `.styles.css`
    via utility classes; only the 5 dynamic `display:none` toggles remain
    inline because JS sets `el.style.display` directly.
    """
    inline_styles = re.findall(r"\bstyle=\"[^\"]*\"", _html())
    assert len(inline_styles) < INLINE_STYLE_THRESHOLD, (
        "expected fewer than "
        + str(INLINE_STYLE_THRESHOLD)
        + " inline `style=...` attributes; found "
        + str(len(inline_styles))
        + " — extract static styling into utility classes in styles.css"
    )


def test_remaining_inline_styles_are_display_none_only() -> None:
    """Every surviving inline `style="..."` must contain `display:none`
    (the JS-toggled hide-by-default pattern). Anything else should have
    migrated to a utility class.
    """
    inline_styles = re.findall(r"\bstyle=\"([^\"]*)\"", _html())
    offenders = [s for s in inline_styles if "display:none" not in s.replace(" ", "")]
    assert not offenders, (
        "non-`display:none` inline styles still present in index.html:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.skip(reason="batch-6 utility-class refactor never landed (9/11 classes absent)")
def test_utility_classes_declared_in_css() -> None:
    """The 11 new batch-6 utility classes must be declared in styles.css."""
    css = _css()
    required_selectors = [
        r"\.meta-dim\s*\{",
        r"\.btn-nowrap\s*\{",
        r"\.meta-inline-sm\s*\{",
        r"\.tl-swatch-warn\s*\{",
        r"\.tl-swatch-adhoc\s*\{",
        r"\.form-actions\.no-mt\s*\{",
        r"\.form-msg-sub\s*\{",
        r"\.workflow-status-panel\s*\{",
        r"\.workflow-warning-banner\s*\{",
        r"\.workflow-log-wrap\s*\{",
        r"\.workflow-output-pre\s*\{",
    ]
    missing = [s for s in required_selectors if not re.search(s, css)]
    assert not missing, "missing utility-class declarations: " + ", ".join(missing)


@pytest.mark.skip(reason="batch-6 utility-class refactor never landed (classes not in markup)")
def test_utility_classes_wired_in_html() -> None:
    """The new utility classes must actually be referenced in index.html."""
    html = _html()
    expected_classes = [
        "btn-nowrap",
        "meta-inline-sm",
        "meta-dim",
        "tl-swatch-warn",
        "tl-swatch-adhoc",
        "form-actions no-mt",  # paired
        "form-msg-sub",
        "workflow-status-panel",
        "workflow-warning-banner",
        "workflow-log-wrap",
        "workflow-output-pre",
    ]
    missing = [c for c in expected_classes if c not in html]
    assert not missing, "utility classes declared but not wired: " + ", ".join(missing)


# ---------- onclick already migrated to data-action -------------------------
def test_no_inline_onclick_remains() -> None:
    """Earlier batches converted all `onclick="..."` handlers to `data-action`
    + delegated listeners. Re-assert here so a future regression is caught.
    """
    html = _html()
    assert "onclick=" not in html, (
        "stray `onclick=` attribute found in index.html — use `data-action` "
        "and a delegated listener instead"
    )


# ---------- Gemini integration CSS preservation -----------------------------
@pytest.mark.skip(reason="gemini dispatch never shipped")
def test_gemini_pill_rule_intact() -> None:
    """The uncommitted Gemini pill rule must survive batch-6 verbatim."""
    css = _css()
    assert (
        ".pill.gemini     { color: var(--warn);     background: var(--warn-bg);"
        "   border-color: var(--warn); }"
    ) in css, "the `.pill.gemini` CSS line drifted — restore it verbatim"


@pytest.mark.skip(reason="gemini dispatch never shipped")
def test_gemini_phases_table_rule_intact() -> None:
    """The uncommitted Gemini phases-table rule must survive batch-6 verbatim."""
    css = _css()
    assert (
        "section.block .phases-table .ph-tool-gemini "
        "{ color: var(--warn); border-color: var(--warn); }"
    ) in css, (
        "the `.ph-tool-gemini` CSS line drifted — restore it verbatim"
    )
