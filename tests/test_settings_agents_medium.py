"""Medium-severity fixes batch for settings.js + agents.js.

Covers four narrow guards that came out of static review:

1. settings.js -- phases CSV string substring match. If the server returned
   `phases: "execute,review"` (string), the previous `current.indexOf(ph) >= 0`
   would behave as substring matching ("reviewer".indexOf("review") === 0)
   and falsely tick the "review" checkbox. Fixed by coercing to an array.

2. settings.js -- postJson silent JSON parse error. The catch block used to
   swallow the parse failure (`/* ignore */`); the fix logs via console.warn
   (or console.error) so the bad response is debuggable without changing
   control flow.

3. agents.js -- countEl null deref. `countEl.textContent = visible.length`
   would throw if the count badge was absent while the block existed.
   Fixed by guarding with `if (countEl)` / `if (!countEl) return`.

4. agents.js -- msg null deref. `msg.textContent = ""` (and subsequent
   assignments) ran without checking msg, so a missing #agent-suggest-msg
   threw. Fixed by guarding with `if (msg)`.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name):
    return (APP / name).read_text(encoding="utf-8")


def test_settings_phases_array_coercion():
    """fillAutoSelect must coerce cfg.phases to an array before .indexOf."""
    src = _src("settings.js")
    assert "Array.isArray(cfg.phases)" in src, (
        "settings.js fillAutoSelect should explicitly coerce cfg.phases "
        "via Array.isArray() to avoid substring matching on CSV strings"
    )
    # And the string branch must exist (split + trim), not just the array branch.
    assert 'typeof cfg.phases === "string"' in src or "cfg.phases.split(" in src, (
        "settings.js should handle the string-CSV case explicitly"
    )


def test_settings_postJson_warns_on_bad_response():
    """postJson catch block must log the parse error (console.warn/error)."""
    src = _src("settings.js")
    # Locate the postJson function body. It's small, so scan a window after the
    # function declaration for either console.warn or console.error.
    m = re.search(r"async function postJson\([^)]*\)\s*\{", src)
    assert m, "postJson function not found in settings.js"
    body = src[m.end(): m.end() + 800]
    assert ("console.warn" in body) or ("console.error" in body), (
        "postJson catch block should surface JSON parse failures via "
        "console.warn or console.error (do not silently ignore)"
    )
    # And the legacy "/* ignore */" silent-swallow must be gone.
    assert "/* ignore */" not in body, (
        "postJson should no longer silently ignore JSON parse failures"
    )


def test_agents_countEl_null_guarded():
    """countEl.textContent must be null-guarded in loadAgentProposals."""
    src = _src("agents.js")
    # Find every `countEl.textContent = ...` write and confirm a guard
    # (`if (countEl)` or `if (!countEl) return`) sits within a small window
    # immediately before it.
    writes = list(re.finditer(r"countEl\.textContent\s*=", src))
    assert writes, "expected countEl.textContent assignments in agents.js"
    for w in writes:
        window = src[max(0, w.start() - 200): w.end()]
        assert (
            "if (countEl)" in window
            or "if (!countEl) return" in window
            or "if (!countEl)" in window
        ), (
            "countEl.textContent assignment at offset "
            f"{w.start()} is not preceded by a null guard"
        )


def test_agents_msg_null_guarded():
    """msg.textContent writes in suggestAgents must be null-guarded."""
    src = _src("agents.js")
    # Restrict to the suggestAgents function body.
    m = re.search(r"async function suggestAgents\([^)]*\)\s*\{", src)
    assert m, "suggestAgents function not found in agents.js"
    # End of function body: balance braces from m.end().
    depth = 1
    i = m.end()
    while i < len(src) and depth > 0:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    body = src[m.end(): i]
    # Every `msg.textContent = ...` write must be guarded by `if (msg)`
    # in a small preceding window.
    writes = list(re.finditer(r"msg\.textContent\s*=", body))
    assert writes, "expected msg.textContent writes inside suggestAgents"
    for w in writes:
        window = body[max(0, w.start() - 120): w.end()]
        assert "if (msg)" in window or "if (!msg) return" in window, (
            "msg.textContent assignment inside suggestAgents at offset "
            f"{w.start()} is not preceded by a null guard"
        )
