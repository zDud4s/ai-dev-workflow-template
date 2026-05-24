"""Static-lint regression tests for settings.js HIGH-severity fixes.

Mirrors tests/test_settings_fixes.py: parses the JS source as text and
asserts presence of marker patterns that encode the four HIGH fixes
applied to .ai/dashboard/app/settings.js:

  1. savePhaseRow checks timeout input validity.valid before POST.
  2. savePhaseRow validates tr.dataset.phase against ALL_PHASES.
  3. renderPhasesTable escapes p.model via escHtml.
  4. The ph-tool-<class> fragment sanitizes `tool` to safe class chars.
"""
import re
from pathlib import Path

SETTINGS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "settings.js"


def _src():
    return SETTINGS_JS.read_text(encoding="utf-8")


def _slice(src, fn_name):
    """Return the body substring of `function fn_name` (sync or async) up to
    the first balanced closing brace at the top level. Best-effort: good
    enough for static linting.
    """
    pat = re.compile(r"(?:async\s+)?function\s+" + re.escape(fn_name) + r"\s*\([^)]*\)\s*\{")
    m = pat.search(src)
    assert m, "expected function " + fn_name + " in settings.js"
    start = m.end()
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return src[start:i]


# ---- Fix 1: timeout input validity check before POST ----
def test_savePhaseRow_checks_timeout_validity():
    body = _slice(_src(), "savePhaseRow")
    # The validity check must come BEFORE the postJson call so an invalid
    # timeout can short-circuit the save.
    post_idx = body.find("postJson")
    assert post_idx > 0, "savePhaseRow must still call postJson"
    pre = body[:post_idx]
    assert "validity.valid" in pre, \
        "savePhaseRow must check tInput.validity.valid before posting"
    # The failure path must surface a user-visible error via setMsg
    # and must `return` to abort the save.
    assert "setMsg(" in pre, \
        "savePhaseRow must call setMsg on invalid timeout before posting"
    # The validity guard must include a return statement before postJson.
    valid_idx = pre.find("validity.valid")
    assert "return" in pre[valid_idx:], \
        "savePhaseRow must return after reporting invalid timeout"


# ---- Fix 2: phase validated against ALL_PHASES ----
def test_savePhaseRow_validates_phase_against_ALL_PHASES():
    body = _slice(_src(), "savePhaseRow")
    has_indexof = "ALL_PHASES.indexOf(" in body
    has_includes = "ALL_PHASES.includes(" in body
    assert has_indexof or has_includes, (
        "savePhaseRow must validate tr.dataset.phase against ALL_PHASES "
        "(via indexOf or includes)"
    )
    # The check must come before postJson so a tampered/missing phase
    # cannot reach the server.
    post_idx = body.find("postJson")
    assert post_idx > 0, "savePhaseRow must still call postJson"
    pre = body[:post_idx]
    assert ("ALL_PHASES.indexOf(" in pre) or ("ALL_PHASES.includes(" in pre), \
        "phase validation must occur before postJson"


# ---- Fix 3: p.model passed through escHtml ----
def test_p_model_uses_escHtml():
    body = _slice(_src(), "renderPhasesTable")
    # Accept either the simple `escHtml(p.model` form or the defensive
    # `escHtml(p && p.model` form.
    has_esc = ("escHtml(p.model" in body) or ("escHtml(p && p.model" in body)
    assert has_esc, (
        "renderPhasesTable must escape p.model via escHtml() before "
        "concatenating it into HTML"
    )
    # Make sure the raw, unescaped pattern is gone — the body should not
    # contain `+ (p.model || ...)` (concatenation without escHtml wrap).
    raw_concat = re.search(r"\+\s*\(p\.model\s*\|\|", body)
    assert raw_concat is None, (
        "found raw p.model concatenation without escHtml() wrap"
    )


# ---- Fix 4: ph-tool-<tool> class attribute sanitized ----
def test_p_tool_class_sanitized():
    src = _src()
    # Accept either restrict-chars sanitizer or escHtml on `tool`.
    has_restrict = bool(re.search(
        r"replace\(\s*/\[\^a-z0-9_\-\]/gi\s*,\s*\"\"\s*\)",
        src,
    ))
    has_escape = "escHtml(tool" in src
    assert has_restrict or has_escape, (
        "the ph-tool-<class> fragment must sanitize `tool` "
        "(via /[^a-z0-9_-]/gi replace OR escHtml())"
    )
    # And the sanitized identifier must actually feed the ph-tool- class.
    # Look for either `ph-tool-' + toolClass` or `ph-tool-' + escHtml(tool`.
    used_in_class = (
        "ph-tool-' + toolClass" in src
        or "ph-tool-\" + toolClass" in src
        or "ph-tool-' + escHtml(tool" in src
    )
    assert used_in_class, (
        "the sanitized tool value must be the one interpolated into "
        "the ph-tool- class attribute"
    )
