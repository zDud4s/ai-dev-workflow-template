"""Static-lint guard: jobs.js must not pass raw server-provided
`tool` strings into pillTool().

Background -- pillTool() lives in core.js:

    function pillTool(tool) {
      const cls = tool === "claude" ? "claude" : ...
      return `<span class="pill ${cls}">${tool || "?"}</span>`;
    }

`${tool}` is interpolated raw (no escape). If a hostile JSON payload
sets a job's `tool` to `<img src=x onerror=alert(1)>` (or any HTML),
the markup lands directly in the DOM via innerHTML.

The defence is a local whitelist (`_jobsSafeTool` / `_safeTool`) that
collapses any unknown value to a sentinel string before it reaches
pillTool. This file asserts the whitelist exists AND that every
pillTool() call in jobs.js routes through it.
"""

from __future__ import annotations

import re
from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "jobs.js"


def _src() -> str:
    return JOBS_JS.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Brace-balanced body of a top-level `function NAME(...)` (incl. braces)."""
    pat = re.compile(r"function\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    assert m, f"function {name!r} not found in jobs.js"
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


def _safe_tool_body() -> str:
    """Return body of whichever helper name exists."""
    src = _src()
    for name in ("_jobsSafeTool", "_safeTool"):
        if re.search(r"function\s+" + re.escape(name) + r"\s*\(", src):
            return _function_body(src, name)
    raise AssertionError("neither _jobsSafeTool nor _safeTool defined in jobs.js")


def test_local_safe_tool_helper_exists():
    """jobs.js must define a whitelist helper (we accept either name)."""
    src = _src()
    has_helper = bool(
        re.search(r'function\s+_jobsSafeTool\s*\(', src)
        or re.search(r'function\s+_safeTool\s*\(', src)
    )
    assert has_helper, (
        "jobs.js should define a local _jobsSafeTool (or _safeTool) "
        "helper to whitelist tool values before pillTool() interpolates"
    )


def test_safe_tool_uses_allowlist():
    """Whitelist must map known names to themselves and collapse the
    rest to a sentinel (e.g. 'unknown')."""
    body = _safe_tool_body()
    assert '"claude"' in body, "_safeTool whitelist must include 'claude'"
    assert '"codex"' in body, "_safeTool whitelist must include 'codex'"
    # Sentinel fallback must exist (any short safe word is fine).
    assert ('"unknown"' in body) or ('"?"' in body) or ('"default"' in body), (
        "_safeTool must collapse unknown values to a safe sentinel string"
    )


def test_pillTool_calls_route_through_safe_tool():
    """Every pillTool() call inside jobs.js must wrap its argument
    in _jobsSafeTool / _safeTool. Raw `pillTool(j.tool)` or
    `pillTool(tool)` is an XSS-tail risk because core.js does not
    escape the value."""
    src = _src()
    # Collect every pillTool(...) call.
    calls = re.findall(r'pillTool\s*\(([^)]+)\)', src)
    assert calls, "expected at least one pillTool(...) call in jobs.js"
    bad = [c.strip() for c in calls
           if "_jobsSafeTool" not in c and "_safeTool" not in c]
    assert not bad, (
        f"pillTool() called with un-whitelisted argument(s) in jobs.js: "
        f"{bad!r}. Wrap each argument in _jobsSafeTool(...) / _safeTool(...) "
        f"to defend against attacker-controlled `tool` fields."
    )


def test_safe_tool_returns_sentinel_for_unknown():
    """A static check that the dict-lookup fallback uses `||` so the
    sentinel actually fires when the input misses the whitelist."""
    body = _safe_tool_body()
    # The classic `{...}[t] || "unknown"` pattern -- accept any || fallback.
    assert "||" in body, (
        "_safeTool must fall back via `|| 'unknown'` (or similar) when the "
        "input is not in the whitelist -- otherwise a missing key yields "
        "undefined and pillTool renders 'undefined' in the markup"
    )
