"""Static-lint tests for skills.js + auto-select.js perf / hardening fixes.

This batch covers four targeted patches:

  1. `renderUnifiedDiff` (skills.js) must cap LCS materialisation before
     allocating an unbounded (n+1)*(m+1) Int32Array. Without the cap, two
     5k-line files balloon to ~200MB in-tab. The fix bails to a non-LCS
     fallback above a per-side line cap (~2000).

  2. `pillTool` (defined in core.js) interpolates its tool string directly
     into a CSS class fragment. skills.js call sites must defensively pass
     only whitelist-approved tool names via a local `_safeTool` helper so
     an attacker-controlled catalog entry can't smuggle in arbitrary class
     tokens.

  3. `openSkillDetail` (skills.js) must use a monotonic epoch counter on
     top of the existing `_currentSkillKey` guard so back-to-back clicks
     on the *same* key (or any future key-collision scheme) still resolve
     to a single winner instead of double-rendering.

  4. `auto-select.js` must use `??` (nullish coalescing) for numeric
     counters like `samples` / `dropped` / `effective`, not `||`, so a
     legitimate 0 from the API is preserved instead of tripping the falsy
     fallback. The spec calls out `samples || "N/A"` as the canonical
     mishandling pattern.

These are pure source-text assertions; no browser is involved.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"
SKILLS_JS = APP / "skills.js"
AUTO_SELECT_JS = APP / "auto-select.js"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _open_skill_detail_body() -> str:
    """Slice openSkillDetail from its declaration to the next top-level
    function declaration so assertions don't bleed into neighbours."""
    src = _src(SKILLS_JS)
    start = src.index("async function openSkillDetail")
    nxt = re.search(r"\n    function\s+\w+\s*\(", src[start:])
    if nxt is None:
        # Could also be terminated by another `async function`.
        nxt2 = re.search(r"\n    async function\s+\w+\s*\(", src[start + 1:])
        end = (start + 1 + nxt2.start()) if nxt2 else len(src)
    else:
        end = start + nxt.start()
    return src[start:end]


def _render_unified_diff_body() -> str:
    """Slice renderUnifiedDiff (and the LCS-cap guard around it) so the
    threshold assertion only matches inside the diff renderer."""
    src = _src(SKILLS_JS)
    # Start a bit BEFORE the function to capture the LCS_LINE_CAP / fallback
    # helpers that live immediately above it.
    cap_marker = "LCS_LINE_CAP"
    start = src.find(cap_marker)
    if start == -1:
        # Fall back to the function header itself.
        start = src.index("function renderUnifiedDiff")
    end_match = re.search(r"\n    function lcsTable\b", src[start:])
    end = start + end_match.start() if end_match else len(src)
    return src[start:end]


# Fix 1 -----------------------------------------------------------------------

def test_lcs_table_has_size_threshold():
    """renderUnifiedDiff (or lcsTable) must bail out before allocating the
    LCS table for unreasonably large files. Without the cap, two 5k-line
    files materialise ~25M Int32 cells (~100MB) in-tab."""
    body = _render_unified_diff_body()
    # Accept either a `.length > 2000` style check or a named constant.
    has_threshold = bool(
        re.search(r"\.length\s*>\s*2000\b", body)
        or re.search(r"LCS_LINE_CAP", body)
    )
    assert has_threshold, (
        "renderUnifiedDiff / lcsTable must cap input size before "
        "allocating the LCS table (e.g. `a.length > 2000` or a named "
        "LCS_LINE_CAP constant). Body excerpt:\n" + body[:600]
    )
    # And there must actually be a fallback path that returns something
    # — not just a silent bail.
    assert "diff too large for LCS" in body or "_diffFallbackForLargeFiles" in body, (
        "Expected a user-visible fallback (e.g. a `_diffFallbackForLargeFiles` "
        "helper or a banner saying `diff too large for LCS`) so the modal "
        "still renders something instead of silently failing."
    )


# Fix 2 -----------------------------------------------------------------------

def test_skills_uses_safeTool():
    """skills.js must define `_safeTool` AND pipe pillTool calls through it
    so an attacker-controlled tool string can't smuggle CSS class tokens
    into the rendered output."""
    src = _src(SKILLS_JS)
    assert re.search(r"function\s+_safeTool\s*\(", src), (
        "skills.js must define a `_safeTool` whitelist helper to sanitise "
        "tool strings before they reach pillTool's class-fragment interpolation."
    )
    # At least one call site must use the helper.
    assert re.search(r"pillTool\s*\(\s*_safeTool\s*\(", src), (
        "skills.js must route at least one `pillTool(...)` call site through "
        "`_safeTool(...)` so the whitelist actually takes effect."
    )
    # And bare `pillTool(s.tool)` / `pillTool(tool)` must be gone — every
    # call site in skills.js needs the wrap. We don't check core/agents/jobs
    # because those are owned by other agents. Use `pillTool(` (no space)
    # so prose mentions like `pillTool (owned by core.js)` don't trip the
    # regex; real call sites never insert a space before `(`.
    bare_calls = re.findall(r"pillTool\((?!_safeTool)[^)]*\)", src)
    assert not bare_calls, (
        "skills.js still has pillTool call sites that don't route through "
        f"_safeTool: {bare_calls!r}"
    )


# Fix 3 -----------------------------------------------------------------------

def test_skill_detail_uses_epoch():
    """openSkillDetail must use a monotonic epoch counter so back-to-back
    clicks always resolve to a single winner — strictly tighter than the
    existing `_currentSkillKey` guard alone (which can collide on identical
    keys)."""
    src = _src(SKILLS_JS)
    body = _open_skill_detail_body()

    # The counter must be declared somewhere in skills.js.
    assert re.search(r"\b_skillDetailEpoch\b", src), (
        "Expected a `_skillDetailEpoch` (or similarly named) counter "
        "declared at module scope in skills.js."
    )

    # openSkillDetail must capture a local epoch and compare it after awaits.
    assert re.search(r"\bepoch\s*=\s*\+\+\s*_skillDetailEpoch\b", body) or re.search(
        r"\b_skillDetailEpoch\s*\+\+", body
    ), (
        "openSkillDetail must increment `_skillDetailEpoch` on entry and "
        "snapshot the new value into a local."
    )

    # At least one post-condition guard comparing the local epoch.
    assert re.search(r"epoch\s*!==?\s*_skillDetailEpoch", body), (
        "openSkillDetail must compare its local epoch against the live "
        "`_skillDetailEpoch` after at least one await, e.g. "
        "`if (epoch !== _skillDetailEpoch) return;`."
    )


# Fix 4 -----------------------------------------------------------------------

def test_auto_select_uses_nullish_coalescing():
    """auto-select.js must use `??` for numeric counter fallbacks so a
    legitimate 0 from the API isn't tripped by the falsy `||` fallback.

    The previous batch's fix used `||` on `data.dropped_candidates` which
    only protected against null — not against 0. This batch tightens it.
    """
    src = _src(AUTO_SELECT_JS)
    assert "??" in src, (
        "auto-select.js source should contain at least one `??` (nullish "
        "coalescing) operator after the batch-2 polish — found none."
    )
    # The specific counter fields the spec calls out must use `??`, not `||`.
    assert re.search(r"data\.samples\s*\?\?", src), (
        "auto-select.js should read `data.samples` via `??` so a 0 value "
        "from the API is preserved (the previous `||` pattern would have "
        "mishandled 0 if the fallback were non-zero)."
    )
    assert re.search(r"data\.dropped_candidates\s*\?\?", src), (
        "auto-select.js should read `data.dropped_candidates` via `??` for "
        "the same reason as `data.samples`."
    )
    assert re.search(r"data\.min_samples\s*\?\?", src), (
        "auto-select.js should read `data.min_samples` via `??` "
        "(this one was already correct in batch 1; the test pins it down)."
    )
