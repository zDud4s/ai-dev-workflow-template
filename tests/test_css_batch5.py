"""Static-lint guards for batch-5 CSS hardening of the workflow dashboard.

Covers:
 - `.toolbar.agents-toolbar` no longer carries `margin-left: 0 !important`
   declarations (the inline styles those overrode are gone, so the fight
   no longer exists).
 - `.toolbar-meta-end` utility class is declared and applies the previously
   inlined `margin-left:auto; color:var(--fg-dim); font-size:var(--fs-2)`.
 - `.form-msg-hint` utility class is declared and applies the previously
   inlined `margin-bottom; color:var(--fg-dim)`.
 - Unused custom properties (`--r-md`, `--r-lg`, `--radius`, `--cut-lg`)
   are removed from `:root`. Confirmed via grep over `.ai/dashboard` for
   `var(--…)` consumers in CSS + JS.
 - `--r-sm` and `--radius-md` remain (each has at least one consumer).
 - `.agent-card .desc` no longer declares the dead-code standard
   `line-clamp:` prop (it requires `display: -webkit-box` to resolve,
   which forces the `-webkit-line-clamp` prefix anyway).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES_CSS = ROOT / ".ai" / "dashboard" / "styles.css"


def _css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _strip_comments(css_fragment: str) -> str:
    """Drop `/* ... */` comments from a CSS fragment so assertions about
    "no X" don't false-positive on documentation that explains why X
    is intentionally absent.
    """
    return re.sub(r"/\*.*?\*/", "", css_fragment, flags=re.DOTALL)


# -- !important removed where inline-style fight is gone ---------------------
def test_agents_toolbar_no_important_overrides() -> None:
    """The two `margin-left: 0 !important` lines in `.toolbar.agents-toolbar`
    existed solely to override an inline `style="margin-left:auto"` on the
    preceding `.meta`. The inline style is gone (moved to `.toolbar-meta-end`),
    so the !important fight should be gone too.
    """
    css = _css()
    # Capture the two agents-toolbar rule bodies and assert no !important.
    for selector in (
        r"\.toolbar\.agents-toolbar\s+#agents-search\s*\{",
        r"\.toolbar\.agents-toolbar\s+#agents-meta\s*\{",
    ):
        match = re.search(selector + r"([^}]*)\}", css)
        assert match, "rule not found in styles.css: " + selector
        body = _strip_comments(match.group(1))
        assert "!important" not in body, (
            "expected no !important in `" + selector + "` rule body"
        )


# -- New utility classes -----------------------------------------------------
def test_toolbar_meta_end_class_declared() -> None:
    """The `.toolbar-meta-end` class must declare margin-left:auto + the
    canonical dim color + a small font-size so the inline-style migration
    is behaviour-preserving.
    """
    css = _css()
    match = re.search(r"\.toolbar-meta-end\s*\{([^}]*)\}", css)
    assert match, "missing `.toolbar-meta-end { … }` rule in styles.css"
    body = match.group(1)
    assert re.search(r"margin-left\s*:\s*auto", body), (
        ".toolbar-meta-end must set margin-left:auto"
    )
    assert "var(--fg-dim)" in body or "var(--text-dim)" in body, (
        ".toolbar-meta-end must use the dim-text token (color)"
    )
    assert "font-size" in body, ".toolbar-meta-end must set font-size"


def test_form_msg_hint_class_declared() -> None:
    """`.form-msg-hint` is the utility class that replaces the inline
    `style="margin-bottom:8px;color:var(--fg-dim)"` on form-msg hints.
    """
    css = _css()
    match = re.search(r"\.form-msg-hint\s*\{([^}]*)\}", css)
    assert match, "missing `.form-msg-hint { … }` rule in styles.css"
    body = match.group(1)
    assert "margin-bottom" in body
    assert "var(--fg-dim)" in body or "var(--text-dim)" in body


# -- Unused custom properties removed ----------------------------------------
def _root_block(css: str) -> str:
    """Return the body of the first `:root { … }` rule, comments stripped
    so docstrings explaining removed tokens don't trip "no X" assertions.
    """
    match = re.search(r":root\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert match, "expected a `:root` rule in styles.css"
    return _strip_comments(match.group(1))


def test_unused_radius_aliases_removed() -> None:
    """`--r-md`, `--r-lg`, `--radius` had zero consumers across
    `.ai/dashboard/**/*.{css,js}`; they were removed from :root. The
    canonical `--radius-md` and the small `--r-sm` (used by terminal
    panes + diff badges) remain.
    """
    root_body = _root_block(_css())
    # Declarations gone.
    assert not re.search(r"(?<!-)--r-md\s*:", root_body), (
        "--r-md should have been removed (no consumers)"
    )
    assert not re.search(r"(?<!-)--r-lg\s*:", root_body), (
        "--r-lg should have been removed (no consumers)"
    )
    # `--radius:` (not `--radius-md:`) must be gone.
    assert not re.search(r"--radius\s*:\s*0\s*;", root_body), (
        "--radius (top-level alias) should have been removed (no consumers)"
    )
    # The canonical names remain.
    assert "--radius-md" in root_body, "--radius-md must remain in :root"
    assert "--r-sm" in root_body, "--r-sm must remain in :root"


def test_unused_cut_lg_removed() -> None:
    """`--cut-lg: 22px` had zero consumers; --cut-sm + --cut-md remain (used
    by the angular `--clip-card` / `--clip-btn` polygons)."""
    root_body = _root_block(_css())
    assert not re.search(r"--cut-lg\s*:", root_body), (
        "--cut-lg should have been removed (no consumers)"
    )
    # Canonical pair remains.
    assert "--cut-sm" in root_body
    assert "--cut-md" in root_body


# -- Removed tokens have no remaining consumers ------------------------------
def test_no_remaining_consumers_of_removed_tokens() -> None:
    """A second guard: nobody references the tokens we just removed
    (`var(--r-md)`, `var(--r-lg)`, `var(--radius)` (NOT `var(--radius-md)`),
    `var(--cut-lg)`) anywhere under `.ai/dashboard/`.
    """
    dashboard_dir = ROOT / ".ai" / "dashboard"
    bad_patterns = [
        re.compile(r"var\(--r-md\b"),
        re.compile(r"var\(--r-lg\b"),
        # var(--radius) but NOT var(--radius-md).
        re.compile(r"var\(--radius\)(?!-)"),
        re.compile(r"var\(--cut-lg\b"),
    ]
    offenders: list[str] = []
    for path in dashboard_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".css", ".js", ".html"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in bad_patterns:
            if pat.search(text):
                offenders.append(str(path) + " :: " + pat.pattern)
    assert not offenders, (
        "removed-token references found:\n  " + "\n  ".join(offenders)
    )


# -- Dead-code line-clamp removed --------------------------------------------
def test_agent_card_desc_no_standard_line_clamp() -> None:
    """`.agent-card .desc` previously declared both `-webkit-line-clamp: 3`
    AND the bare standard `line-clamp: 3`. The standard prop is dead code
    on the open web in 2026 (it only resolves with `display: -webkit-box`,
    which already forces the `-webkit-` prefix). Status doc line 143
    flagged this; remove it so the rule doesn't claim coverage it can't
    deliver.
    """
    css = _css()
    match = re.search(r"\.agent-card\s+\.desc\s*\{([^}]*)\}", css, flags=re.DOTALL)
    assert match, "missing `.agent-card .desc { … }` rule"
    body_raw = match.group(1)
    # webkit-prefixed line-clamp must still be there (in declarations, not
    # the comment).
    body = _strip_comments(body_raw)
    assert "-webkit-line-clamp" in body
    # The bare standard prop (NOT preceded by `-webkit-`) must be gone from
    # the actual declarations. We strip comments first so the explanatory
    # `/* ... line-clamp ... */` block (which is intentionally there) does
    # not false-positive.
    assert not re.search(
        r"(?<!-webkit-)line-clamp\s*:", body
    ), ".agent-card .desc still declares the dead-code standard `line-clamp:`"
