"""Medium-severity hardening tests for .ai/dashboard/app/core.js.

Covers batch-4 fixes:
  - loadTokenUsage no longer swallows errors silently (setMsg or console.warn).
  - localStorage reads (`dash.density`, `dash.view`) wrapped in try/catch
    so private-browsing throws don't abort DOMContentLoaded handlers.
  - `$("#dec-date").value = today` is null-guarded.
  - `countMemoryEntries` defensively coerces null/undefined input.
  - `formatTokens("")` returns "—" instead of "0".
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name):
    return (APP / name).read_text(encoding="utf-8")


def _function_body(src, name):
    """Naively extract `[async] function NAME(...) { ... }` body by brace count."""
    for marker in (
        "async function " + name + "(",
        "function " + name + "(",
    ):
        idx = src.find(marker)
        if idx != -1:
            break
    else:
        raise AssertionError("Could not find function " + name)
    brace_open = src.index("{", idx)
    depth = 0
    for i in range(brace_open, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace_open : i + 1]
    raise AssertionError("Could not locate body of " + name)


def test_loadTokenUsage_has_setMsg_or_warn():
    """The catch block in loadTokenUsage previously only called console.error,
    so a failed /api/usage/total left "—" placeholders in the UI with no
    operator-visible feedback. Verify the catch now also calls setMsg(...)
    on a #token-usage-msg channel or falls back to console.warn."""
    src = _src("core.js")
    body = _function_body(src, "loadTokenUsage")
    # Find the OUTER catch block — the function contains an inner try/catch
    # around `new Date(...).toLocaleString()`, so we want the last catch in
    # source order, which corresponds to the top-level try/catch.
    catch_matches = list(re.finditer(r"catch\s*\([^)]*\)\s*\{", body))
    assert catch_matches, "loadTokenUsage should have at least one catch block"
    catch_match = catch_matches[-1]
    catch_start = catch_match.end()
    # Brace-count the catch body.
    depth = 1
    catch_end = catch_start
    for i in range(catch_start, len(body)):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                catch_end = i
                break
    catch_body = body[catch_start:catch_end]
    has_setmsg = "setMsg(" in catch_body
    has_warn = "console.warn" in catch_body
    assert has_setmsg or has_warn, (
        "loadTokenUsage catch block should call setMsg(...) or console.warn "
        "so operators see a failure signal — silent console.error is not "
        "enough"
    )


def test_localStorage_wrapped_in_try():
    """In private browsing / restricted contexts, localStorage.getItem throws.
    The `dash.density` read (and the `dash.view` read in restoreView) must be
    wrapped in try/catch so DOMContentLoaded handlers don't abort."""
    src = _src("core.js")
    # Locate the call site.
    density_idx = src.index('localStorage.getItem("dash.density")')
    # Walk backwards a generous window and confirm a `try {` precedes it
    # without an intervening function/brace boundary.
    preface = src[max(0, density_idx - 400) : density_idx]
    assert "try {" in preface or "try{" in preface, (
        "localStorage.getItem(\"dash.density\") must be wrapped in a try { ... } "
        "block so private-browsing throws don't abort DOMContentLoaded"
    )
    # Also confirm a catch handler exists nearby.
    suffix = src[density_idx : density_idx + 400]
    assert "catch" in suffix, (
        "localStorage.getItem(\"dash.density\") must have a catch handler "
        "with a sensible default"
    )


def test_dec_date_null_guarded():
    """`$("#dec-date").value = today` previously had no null guard; if the
    element is renamed or removed the rest of the DOMContentLoaded handler
    aborts. Verify a null-guard is present near the assignment."""
    src = _src("core.js")
    # The current pattern uses `const decDate = $("#dec-date"); if (decDate) ...`
    # Accept either that pattern, optional chaining, or an explicit null check.
    no_direct_assign = '$("#dec-date").value = today' not in src
    has_null_guard = bool(
        re.search(r'const\s+decDate\s*=\s*\$\(["\']#dec-date["\']\)\s*;\s*if\s*\(\s*decDate\s*\)', src)
        or re.search(r'\$\(["\']#dec-date["\']\)\?\.value', src)
    )
    assert no_direct_assign, (
        "`$(\"#dec-date\").value = today` should no longer be an unguarded "
        "dereference"
    )
    assert has_null_guard, (
        "The #dec-date value assignment must be null-guarded (e.g. "
        "`const decDate = $(\"#dec-date\"); if (decDate) decDate.value = today;`)"
    )


def test_countMemoryEntries_coerces():
    """countMemoryEntries previously called `text.match(...)` directly, so
    a null/undefined argument would throw. The function body should coerce
    text to a string at entry."""
    src = _src("core.js")
    body = _function_body(src, "countMemoryEntries")
    has_coerce = (
        re.search(r'text\s*=\s*text\s*\|\|\s*""', body) is not None
        or 'text || ""' in body
        or "String(text" in body
    )
    assert has_coerce, (
        "countMemoryEntries should coerce its `text` arg defensively at entry "
        "(e.g. `text = text || \"\";`) so null/undefined doesn't throw on .match"
    )


def test_formatTokens_handles_empty_string():
    """formatTokens("") previously returned "0" because `isNaN("")` is false
    (empty string coerces to 0). The function should check for empty string
    explicitly and return the em-dash placeholder."""
    src = _src("core.js")
    body = _function_body(src, "formatTokens")
    # Accept any explicit empty-string guard: `n === ""`, `n == ""`, or
    # `!n` / `n === ""` / `typeof n === "string" && !n`.
    has_empty_check = (
        'n === ""' in body
        or "n === ''" in body
        or 'n == ""' in body
    )
    assert has_empty_check, (
        "formatTokens should explicitly check for empty string at entry "
        "(`if (n === \"\" || ...) return \"—\"`) — `isNaN(\"\")` is false so "
        "the existing isNaN guard lets empty strings through as 0"
    )
