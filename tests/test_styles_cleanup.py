"""Cleanup-batch regression tests for the dashboard stylesheet.

Locks in four low-risk hygiene fixes applied in this batch:

  1. Dead CSS alias `--mono` removed from `:root` (no `var(--mono)` reads
     existed). Sibling dead aliases (`--teal`, `--glass-2`, `--glass-3`) were
     removed in the same pass; we assert on `--mono` as the canary.

  2. The `.cards .card:nth-child(N)` animation-delay staircase used to cliff
     at n+7 (all cards 7..N shared one delay, collapsing the streaming
     feel). The staircase now extends through n+11 with a final flatten at
     `:nth-child(n+12)`. We assert at least one of the new mid-range
     selectors (e.g. `:nth-child(8)`) is present.

  3. `#view-timeline` previously used legacy aliases (`--fg`, `--fg-dim`).
     Five references were rewritten to canonical (`--text`, `--text-dim`).
     We assert the count of `var(--fg)` / `var(--fg-dim)` references inside
     the `#view-timeline` block is below the previous baseline.

  4. Regression guard: `.diff-line {` and `.term-head .id {` remain
     single-defined after the duplicate-rule consolidation in this batch.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "styles.css"


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _rule_block(selector: str, css: str) -> str:
    match = re.search(rf"(?m)^[ \t]*{re.escape(selector)}\s*\{{[^}}]*\}}", css)
    assert match, f"{selector} rule not found in styles.css"
    return match.group(0)


def test_dur_fast_declared() -> None:
    css = _css()
    root = _rule_block(":root", css)
    assert re.search(r"--dur-fast\s*:", root), "--dur-fast must be declared in :root"


def test_counter_reset_on_view() -> None:
    css = _css()
    main = _rule_block("main", css)
    view = _rule_block(".view", css)
    assert "counter-reset: section-counter" in view
    assert "counter-reset: section-counter" not in main


def test_toast_demoted_under_modal() -> None:
    css = _css()
    assert re.search(
        r"body:has\(\.proposal-modal:not\(\[hidden\]\)\)\s+#toast-root\s*\{"
        r"[^}]*z-index:\s*150\s*;",
        css,
        re.DOTALL,
    ), "toast root must sit below the open proposal modal"


# ---------------------------------------------------------------------------
# Fix 1: dead `--mono` alias removed
# ---------------------------------------------------------------------------
def test_no_dead_mono_alias() -> None:
    """The dead `--mono: var(--ff-mono);` declaration must be gone.

    `--ff-mono` (the canonical font-family token) is still declared and
    referenced; only the unused short-form alias is dropped.
    """
    css = _css()
    # No declaration line that reads `--mono: ...;` (note the trailing colon).
    # Be tolerant of any whitespace between the name and the colon.
    assert re.search(r"--mono\s*:", css) is None, (
        "Found a `--mono:` declaration; this alias was never read via "
        "`var(--mono)` and should have been removed in the cleanup pass"
    )
    # Canonical font-family token must still be present and used.
    assert "--ff-mono:" in css
    assert "var(--ff-mono" in css


# ---------------------------------------------------------------------------
# Fix 2: card animation staircase extends past n+7
# ---------------------------------------------------------------------------
def test_card_animation_extended_beyond_n7() -> None:
    """The streaming-card stagger must continue past the previous n+7 cliff."""
    css = _css()
    # The previous cliff selector `:nth-child(n+7)` should be replaced by
    # explicit per-card delays through at least :nth-child(8), with a final
    # `:nth-child(n+12)` (or higher) flattening selector.
    assert re.search(
        r"\.cards \.card:nth-child\(8\)\s*\{[^}]*animation-delay",
        css,
    ), "expected an explicit :nth-child(8) animation-delay rule"
    # A trailing flatten selector >= n+12 should exist so we don't stagger
    # forever on large grids.
    assert re.search(
        r"\.cards \.card:nth-child\(n\+1[2-9]\)\s*\{",
        css,
    ), "expected a `:nth-child(n+12)` (or later) flatten rule"


def test_card_after_integer_offsets() -> None:
    """The card beacon offsets should avoid sub-pixel blur on 1x displays."""
    css = _css()
    block = _rule_block(".card::after", css)
    assert re.search(r"bottom\s*:\s*0\s*;", block)
    assert re.search(r"left\s*:\s*-2px\s*;", block)
    assert not re.search(r"(?:bottom|left)\s*:\s*-?(?:\d*\.)\d+px\s*;", block)


def test_run_form_options_align_start() -> None:
    """Run-form options should align labels at the top of the row."""
    css = _css()
    block = _rule_block(".run-form .form-row.run-form-options", css)
    assert "align-items: start" in block
    assert "align-items: end" not in block


def test_card_cascade_capped_or_extended() -> None:
    """Cards 12+ must have an intentional animation-delay rule."""
    css = _css()
    capped = re.search(
        r"\.cards \.card:nth-child\(n\+12\)\s*\{[^}]*"
        r"animation-delay\s*:\s*0(?:ms)?\s*;",
        css,
    )
    extended = re.search(
        r"\.cards \.card:nth-child\((?:1[2-9]|20)\)\s*\{[^}]*"
        r"animation-delay",
        css,
    )
    assert capped or extended, (
        "expected an intentional n+12 zero-delay cap or explicit nth-child "
        "rules beyond 11"
    )


# ---------------------------------------------------------------------------
# Fix 3: timeline uses canonical tokens
# ---------------------------------------------------------------------------
def _timeline_block(css: str) -> str:
    """Return the CSS text from the start of the timeline section to the
    next major section marker. Used to count legacy var() references in
    isolation.
    """
    # Anchor on the section comment that introduces the timeline rules.
    start = css.find("/* ---------------- Timeline view (Gantt) ----------------")
    assert start != -1, "timeline section marker not found in styles.css"
    # End at the next top-level section comment ("===== Run page rework").
    end = css.find("===== Run page rework", start)
    assert end != -1, "expected a section terminator after the timeline block"
    return css[start:end]


def test_view_timeline_uses_canonical_tokens() -> None:
    """The `#view-timeline` block was previously littered with legacy
    `--fg` / `--fg-dim` references. The cleanup pass converted at least
    five of them to canonical `--text` / `--text-dim`.
    """
    block = _timeline_block(_css())
    legacy_fg = len(re.findall(r"var\(--fg\)", block))
    legacy_fg_dim = len(re.findall(r"var\(--fg-dim\)", block))
    # Previous baseline (pre-cleanup) was: --fg x2, --fg-dim x7 (=9 total).
    # After the sweep we expect <=4 total remaining inside the timeline block.
    assert (legacy_fg + legacy_fg_dim) <= 4, (
        f"timeline block still contains {legacy_fg} var(--fg) and "
        f"{legacy_fg_dim} var(--fg-dim) references; expected the canonical "
        "--text / --text-dim tokens to dominate"
    )
    # Canonical tokens must have appeared at least 4 times in the block.
    canonical = len(re.findall(r"var\(--text(?:-dim)?\)", block))
    assert canonical >= 4, (
        f"expected at least 4 canonical --text / --text-dim references in "
        f"the timeline block; found {canonical}"
    )


# ---------------------------------------------------------------------------
# Fix 4: regression — single-def selectors stay single
# ---------------------------------------------------------------------------
def _count_rule_starts(selector: str, css: str) -> int:
    return len(re.findall(re.escape(selector) + r"\s*\{", css))


def test_diff_line_term_head_id_still_single_def() -> None:
    """Regression check: prior dedupe of `.diff-line` and `.term-head .id`
    must survive this batch's duplicate-rule consolidation pass.
    """
    css = _css()
    assert _count_rule_starts(".diff-line", css) == 1, (
        ".diff-line should still have exactly one rule-opening `{`"
    )
    assert _count_rule_starts(".term-head .id", css) == 1, (
        ".term-head .id should still have exactly one rule-opening `{`"
    )
