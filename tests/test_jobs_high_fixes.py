import re
from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "jobs.js"


def _src():
    return JOBS_JS.read_text(encoding="utf-8")


def _function_body(src, name):
    """Return the source of the named function (best-effort brace-match)."""
    pat = re.compile(r"function\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    assert m, f"function {name!r} not found in jobs.js"
    # Find the opening brace after the signature.
    i = src.find("{", m.end())
    assert i != -1, f"opening brace for {name!r} not found"
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(f"could not find end of function {name!r}")


def test_loadEvents_logs_dropped_lines():
    """loadEvents must count malformed JSONL lines and warn the user."""
    body = _function_body(_src(), "loadEvents")
    assert "console.warn" in body, \
        "loadEvents must call console.warn to surface dropped malformed lines"
    assert "dropped" in body, \
        "loadEvents warn message should mention 'dropped' so users see data loss"


def test_event_expand_key_is_content_stable():
    """Flat-event expand key must NOT use the array index — that shifts on refresh."""
    body = _function_body(_src(), "_evRenderFlat")
    # Look for the actual assignment line(s) and ensure no bare idx token is interpolated.
    expand_lines = [
        ln for ln in body.splitlines()
        if "expandKey" in ln and "=" in ln and "//" not in ln.split("=")[0]
    ]
    assert expand_lines, "expandKey assignment must exist in _evRenderFlat"
    for ln in expand_lines:
        assert not re.search(r"\$\{\s*idx\s*\}", ln), \
            f"expandKey must not interpolate the array index (line: {ln.strip()!r})"
    # Positive check: should reference session_id (or e.session_id).
    joined = "\n".join(expand_lines)
    assert "session_id" in joined, \
        "expandKey should incorporate e.session_id (or similar content) for stability"


def test_safe_parse_date_helper_exists():
    """A module-local helper must exist that explicitly handles invalid Date.parse input."""
    src = _src()
    assert "_safeParseDate" in src, \
        "_safeParseDate helper (or equivalent) must be defined to wrap Date.parse"
    # And the helper definition must be present (not only call sites).
    assert re.search(r"function\s+_safeParseDate\s*\(", src), \
        "_safeParseDate must be defined as a function in jobs.js"


def test_evRenderGrouped_guards_missing_ts():
    """_evRenderGrouped must guard against first.ts being undefined/empty."""
    body = _function_body(_src(), "_evRenderGrouped")
    has_guard = (
        "first.ts ?" in body
        or "first.ts ||" in body
        or re.search(r"if\s*\(\s*first\.ts", body) is not None
        or re.search(r"const\s+tsStr\s*=\s*first\.ts", body) is not None
    )
    assert has_guard, \
        "_evRenderGrouped must guard against missing first.ts before passing to relativeTime/new Date"
