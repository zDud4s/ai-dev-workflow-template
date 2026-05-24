"""Static-lint regression tests for terminals.js batch-8 LOW + PERF fixes.

Scope: residual LOW + PERF bugs flagged in ``docs/bug-hunt-status.md``
for ``.ai/dashboard/app/terminals.js`` after batches 1-7. Targets:

  PERF — already-closed regression guards (verified still in place):
    * `simpleLineDiff` cell cap below the cliff (`> 200_000` lowered to
      `100_000` in batch 2; now anchored to a named constant
      `SIMPLE_LINE_DIFF_CELL_CAP`).
    * `t.body.normalize()` gated behind `t._searchActive` flag
      (batch 2 — must not regress).
    * Search input debounce wired with setTimeout/clearTimeout
      (batch 2 — must not regress).
    * TreeWalker scan inside `termRunSearch` capped per call by
      `TERM_SEARCH_NODE_CAP` so a 50K-node pane can't make a single
      search call burn 100+ms (new defensive cap added this batch).

  LOW — new fixes in this batch:
    1. `isCodex` regex inside `termOpenDispatchTracker` was missing the
       `(\\s|$)` word-end anchor that its sibling
       `termIsLLMDispatchCommand` uses — could mis-label a Bash command
       containing `codex executor` as a Codex dispatch.
    2. `--input-format stream-json` regex inside
       `termIsLLMDispatchCommand` was missing the `/i` flag that its
       `-p/--print` sibling carries, so a mixed-case
       `--Input-Format Stream-Json` invocation slipped past detection.
    3. Magic number `220` (composer textarea max height) appeared at
       five sites — extracted to module-level
       `COMPOSER_AUTOSIZE_MAX_PX`.
    4. Magic number `4000` (toast duration) appeared at 17 sites —
       extracted to module-level `TERM_MSG_DURATION_MS`.

Pattern mirrors ``tests/test_terminals_batch7.py`` and
``tests/test_terminals_perf.py``.
"""
from __future__ import annotations

import re
from pathlib import Path


TERMINALS_JS = (
    Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "terminals.js"
)


def _src() -> str:
    return TERMINALS_JS.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """Return the body of the first function whose signature matches ``header``."""
    idx = src.find(header)
    assert idx != -1, f"could not locate {header!r} in terminals.js"
    brace = src.find("{", idx)
    assert brace != -1
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
    raise AssertionError(f"unterminated function body for {header!r}")


# ---------------------------------------------------------------------------
# LOW Fix 1 — isCodex regex anchored with (\s|$)
# ---------------------------------------------------------------------------


def test_dispatch_tracker_isCodex_regex_has_word_end_anchor():
    """``termOpenDispatchTracker`` builds an ``isCodex`` flag from the
    Bash command — its regex must mirror the
    ``termIsLLMDispatchCommand`` pattern with ``(\\s|$)`` so a command
    starting with ``codex executor`` (or any other identifier prefixed
    by ``codex exec``) is not mis-labelled as a Codex dispatch.
    """
    body = _slice_function(_src(), "function termOpenDispatchTracker(")
    # The bare-`exec` pattern (no anchor) must be gone from this site.
    assert "/\\bcodex\\s+exec/.test(cmd)" not in body, (
        "termOpenDispatchTracker's isCodex regex regressed to the "
        "anchorless ``/\\bcodex\\s+exec/`` pattern — a Bash command "
        "containing ``codex executor`` would be mis-labelled as Codex"
    )
    # The anchored form must be present.
    assert "/\\bcodex\\s+exec(\\s|$)/" in body, (
        "termOpenDispatchTracker's isCodex must use the anchored "
        "``/\\bcodex\\s+exec(\\s|$)/`` pattern matching its sibling "
        "in termIsLLMDispatchCommand"
    )


# ---------------------------------------------------------------------------
# LOW Fix 2 — --input-format stream-json regex is case-insensitive
# ---------------------------------------------------------------------------


def test_input_format_stream_json_regex_is_case_insensitive():
    """The ``--input-format stream-json`` arm inside
    ``termIsLLMDispatchCommand`` must carry the ``/i`` flag that its
    sibling ``-p/--print`` line uses; Windows users frequently invoke
    the CLI with mixed-case flags."""
    body = _slice_function(_src(), "function termIsLLMDispatchCommand(")
    # We expect the regex literal to end with ``/i``.
    has_i_flag = bool(
        re.search(r"/\\bclaude.*--input-format\\s\+stream-json/i", body)
    )
    assert has_i_flag, (
        "termIsLLMDispatchCommand's --input-format regex must carry "
        "the /i flag so mixed-case flag names still trip detection"
    )


# ---------------------------------------------------------------------------
# LOW Fix 3 — composer autosize max extracted from literal 220
# ---------------------------------------------------------------------------


def test_composer_autosize_max_is_a_named_constant():
    """The five textarea-autosize sites must reference the
    ``COMPOSER_AUTOSIZE_MAX_PX`` module-level constant rather than
    the literal ``220`` (which conflated UX intent with a magic
    number wherever it appeared)."""
    src = _src()
    # The constant must be declared.
    assert re.search(
        r"var\s+COMPOSER_AUTOSIZE_MAX_PX\s*=\s*220",
        src,
    ), "COMPOSER_AUTOSIZE_MAX_PX module-level constant must be declared as 220"
    # The literal ``Math.min(...scrollHeight, 220)`` form must be gone
    # everywhere except the constant declaration itself.
    leftover = re.findall(r"Math\.min\([^,]+\.scrollHeight,\s*220\s*\)", src)
    assert not leftover, (
        f"{len(leftover)} site(s) still use ``Math.min(..., 220)`` — "
        "swap to ``COMPOSER_AUTOSIZE_MAX_PX`` so future UX tweaks are "
        "single-line affairs"
    )
    # And the named-constant form must be present at least 3 times
    # (the file has 5 composers; we accept >=3 to allow conservative
    # rollout).
    refs = len(re.findall(r"COMPOSER_AUTOSIZE_MAX_PX", src))
    assert refs >= 4, (
        f"only {refs} references to COMPOSER_AUTOSIZE_MAX_PX — expected "
        ">=4 (declaration + at least 3 use sites)"
    )


# ---------------------------------------------------------------------------
# LOW Fix 4 — toast duration extracted from literal 4000
# ---------------------------------------------------------------------------


def test_setMsg_duration_is_a_named_constant():
    """``setMsg(..., 4000)`` appeared at 17 sites. The literal
    must be replaced by ``TERM_MSG_DURATION_MS`` so subsequent UX
    tweaks (e.g. shorter durations for snappier feedback) are
    one-line affairs."""
    src = _src()
    # The constant must be declared as 4000.
    assert re.search(
        r"var\s+TERM_MSG_DURATION_MS\s*=\s*4000",
        src,
    ), "TERM_MSG_DURATION_MS module-level constant must be declared as 4000"
    # No literal `, 4000)` call sites should remain (the only `4000`
    # references must be inside the declaration/comment block).
    literal_sites = re.findall(r",\s*4000\s*\)", src)
    assert not literal_sites, (
        f"{len(literal_sites)} site(s) still pass the literal 4000 to "
        "setMsg — swap to TERM_MSG_DURATION_MS"
    )
    refs = len(re.findall(r"TERM_MSG_DURATION_MS", src))
    assert refs >= 10, (
        f"only {refs} references to TERM_MSG_DURATION_MS — expected at "
        "least 10 (declaration + many setMsg sites)"
    )


# ---------------------------------------------------------------------------
# PERF Fix — TreeWalker scan capped per call
# ---------------------------------------------------------------------------


def test_termRunSearch_treewalker_has_node_cap():
    """The TreeWalker scan inside ``termRunSearch`` must be capped per
    invocation so a chat pane that's been streaming for an hour (tens
    of thousands of text nodes) can't make a single search call burn
    100+ms even after the existing 150ms input debounce."""
    src = _src()
    body = _slice_function(src, "function termRunSearch(")
    # The cap constant must be declared somewhere in the file.
    assert re.search(
        r"var\s+TERM_SEARCH_NODE_CAP\s*=\s*\d+",
        src,
    ), "TERM_SEARCH_NODE_CAP module-level constant must be declared"
    # The while-loop body inside termRunSearch must reference the cap.
    assert "TERM_SEARCH_NODE_CAP" in body, (
        "termRunSearch's TreeWalker scan must reference "
        "TERM_SEARCH_NODE_CAP so the walk is bounded per call"
    )
    # And there must be a break statement in the loop (the cap
    # must actually halt iteration, not just be referenced).
    assert "break" in body, (
        "termRunSearch's bounded TreeWalker loop must use ``break`` "
        "(or equivalent) once the node cap is exceeded"
    )


# ---------------------------------------------------------------------------
# PERF Regression guards (batch 2 fixes must still survive)
# ---------------------------------------------------------------------------


def test_simpleLineDiff_cell_cap_uses_named_constant():
    """REGRESSION (batch 2 + this batch) — the cliff guard inside
    ``simpleLineDiff`` must short-circuit before allocating the DP
    grid AND reference the module-level
    ``SIMPLE_LINE_DIFF_CELL_CAP`` constant instead of a bare literal
    so the size constraint is rediscoverable from a grep."""
    src = _src()
    body = _slice_function(src, "function simpleLineDiff(")
    # The constant must exist.
    assert re.search(
        r"var\s+SIMPLE_LINE_DIFF_CELL_CAP\s*=\s*100[_]?000",
        src,
    ), "SIMPLE_LINE_DIFF_CELL_CAP must be declared at 100_000"
    # The guard must reference the constant, not the literal.
    assert "SIMPLE_LINE_DIFF_CELL_CAP" in body, (
        "simpleLineDiff's cliff guard must reference "
        "SIMPLE_LINE_DIFF_CELL_CAP, not a bare 100_000 literal"
    )
    # The literal `> 100000` form must be gone from this function.
    assert "> 100000" not in body, (
        "simpleLineDiff still uses the bare ``> 100000`` literal — "
        "swap to ``> SIMPLE_LINE_DIFF_CELL_CAP``"
    )


def test_body_normalize_still_gated_behind_active_flag():
    """REGRESSION (batch 2) — ``t.body.normalize()`` must remain
    inside an ``if (t._searchActive)`` branch so a keystroke that hits
    the clear path before any highlights existed doesn't pay the O(n)
    DOM walk."""
    body = _slice_function(_src(), "function termClearSearchHighlights(")
    # The gate must be present.
    assert "_searchActive" in body, (
        "termClearSearchHighlights regressed — the normalize() call "
        "must remain gated behind ``if (t._searchActive)``"
    )
    # And the normalize call must be inside an if block.
    has_gated_normalize = bool(
        re.search(
            r"if\s*\(\s*t\._searchActive\s*\)\s*\{\s*\n\s*t\.body\.normalize\(\)",
            body,
        )
    )
    assert has_gated_normalize, (
        "termClearSearchHighlights regressed — the normalize() call "
        "must appear immediately inside an ``if (t._searchActive)`` "
        "block, not as a bare always-runs statement"
    )


def test_search_input_listener_still_debounced():
    """REGRESSION (batch 2) — the in-pane search input listener must
    debounce keystrokes via setTimeout/clearTimeout so a fast typist
    doesn't trigger a fresh TreeWalker scan per character."""
    src = _src()
    anchor_idx = src.find('searchInput.addEventListener("input"')
    assert anchor_idx != -1, "search input listener anchor missing"
    region = src[anchor_idx : anchor_idx + 400]
    assert "setTimeout" in region, (
        "search input listener regressed — debounce setTimeout missing"
    )
    assert "clearTimeout" in region, (
        "search input listener regressed — clearTimeout (reset) missing"
    )


# ---------------------------------------------------------------------------
# Bundle sanity
# ---------------------------------------------------------------------------


def test_terminals_js_non_empty_and_capped():
    """A truncating edit usually halves the file. Belt-and-braces."""
    src = _src()
    assert len(src) > 100_000, (
        f"terminals.js shrank to {len(src)} bytes — likely truncation"
    )
    assert src.lstrip().startswith("// .ai/dashboard/app/terminals.js"), (
        "header comment lost — likely destructive top-of-file edit"
    )
    assert "restoreOpenPanes" in src[-2000:], (
        "tail boot sequence lost — likely truncation"
    )
