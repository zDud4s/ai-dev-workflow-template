"""Source-level contract for the memory-page token-budget ring.

The memory view renders a floating double-ring ("ball") over `#memory-doc`,
echoing Claude Code's context-window / compact indicator:

* outer arc = estimated tokens (`chars / 4`) vs a ~2000-token budget — the
  primary signal, which drives the colour state;
* inner arc = line count vs the consolidation threshold
  (`memory_tuning.consolidation_threshold_lines` in project.yaml, default 150).

Everything is computed client-side from data the page already loads, so there
is no backend endpoint to exercise. These tests pin the source-level wiring so
a future refactor can't silently drop the element, the render calls, or the
budget/threshold contract. The path literals below also couple this group to
the dashboard files via the test catalog's static scanner.
"""
from __future__ import annotations

from pathlib import Path
import math

import pytest

from conftest import REPO_ROOT


INDEX_HTML = REPO_ROOT / ".ai" / "dashboard" / "index.html"
CORE_JS = REPO_ROOT / ".ai" / "dashboard" / "app" / "core.js"
MAIN_JS = REPO_ROOT / ".ai" / "dashboard" / "app" / "main.js"
MISC_CSS = REPO_ROOT / ".ai" / "dashboard" / "styles" / "misc.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Markup                                                                       #
# --------------------------------------------------------------------------- #

def test_index_has_ball_element_and_wrapper():
    html = _read(INDEX_HTML)
    # The wrapper provides the positioning context; the ball is its sibling of
    # #memory-doc so renderMarkdown's innerHTML replacement never wipes it.
    assert 'class="mem-doc-wrap"' in html
    assert 'id="memory-budget"' in html
    # Sibling, not child: the ball must appear before #memory-doc inside wrap.
    assert html.index('id="memory-budget"') < html.index('id="memory-doc"')


def test_ball_is_labelled_for_a11y():
    html = _read(INDEX_HTML)
    # role="img" + an aria-label make the ring announceable; JS keeps the
    # label text current, but the static fallback must exist.
    block = html[html.index('id="memory-budget"') - 80:
                 html.index('id="memory-budget"') + 120]
    assert 'role="img"' in block
    assert "aria-label" in block


# --------------------------------------------------------------------------- #
# Render wiring                                                                #
# --------------------------------------------------------------------------- #

def test_core_defines_render_and_stats():
    src = _read(CORE_JS)
    assert "function renderMemoryBudget(" in src
    assert "function memoryBudgetStats(" in src
    assert "function memoryBudgetLineCount(" in src


def test_render_called_on_load_and_after_append():
    # Initial load passes the parsed project so the threshold is read from
    # project.yaml; the post-append call reuses the cached project.
    assert "renderMemoryBudget(memoryText, project)" in _read(MAIN_JS)
    assert "renderMemoryBudget(memText)" in _read(CORE_JS)


# --------------------------------------------------------------------------- #
# Budget / threshold contract                                                 #
# --------------------------------------------------------------------------- #

def test_token_budget_constant_is_2000():
    src = _read(CORE_JS)
    assert "MEM_TOKEN_BUDGET = 2000" in src


def test_token_estimate_is_chars_over_four():
    # chars/4 is the maintenance skill's own token heuristic.
    assert "Math.ceil(text.length / 4)" in _read(CORE_JS)


def test_line_budget_reads_threshold_with_default_150():
    src = _read(CORE_JS)
    assert "consolidation_threshold_lines" in src
    assert "MEM_DEFAULT_LINE_BUDGET = 150" in src


def test_colour_state_thresholds_present():
    # over at >=100% of the token budget, warn at >=70%, else ok.
    src = _read(CORE_JS)
    assert "s.tokenPct >= 1" in src
    assert "s.tokenPct >= 0.7" in src
    assert '"over"' in src and '"warn"' in src and '"ok"' in src


def test_css_styles_the_ring_states():
    css = _read(MISC_CSS)
    assert ".mem-budget" in css
    assert ".mem-doc-wrap" in css
    # Each colour state maps to a semantic token.
    assert 'data-state="over"' in css and "var(--bad)" in css
    assert 'data-state="warn"' in css and "var(--warn)" in css
    assert 'data-state="ok"' in css and "var(--ok)" in css


# --------------------------------------------------------------------------- #
# Math contract — re-derives the JS formula so the displayed numbers are       #
# pinned independently of the source-string assertions above.                  #
# --------------------------------------------------------------------------- #

TOKEN_BUDGET = 2000


def _state(token_pct: float) -> str:
    if token_pct >= 1:
        return "over"
    if token_pct >= 0.7:
        return "warn"
    return "ok"


@pytest.mark.parametrize(
    "chars, expected_tokens, expected_pct_label, expected_state",
    [
        (0, 0, 0, "ok"),
        (4000, 1000, 50, "ok"),          # half budget
        (5600, 1400, 70, "warn"),        # exactly the warn threshold
        (7996, 1999, 100, "warn"),       # just under budget, rounds to 100 but < 1.0
        (8000, 2000, 100, "over"),       # at budget
        (9440, 2360, 118, "over"),       # over budget
    ],
)
def test_token_math_contract(chars, expected_tokens, expected_pct_label, expected_state):
    tokens = math.ceil(chars / 4)
    assert tokens == expected_tokens
    token_pct = tokens / TOKEN_BUDGET
    assert round(token_pct * 100) == expected_pct_label
    assert _state(token_pct) == expected_state
