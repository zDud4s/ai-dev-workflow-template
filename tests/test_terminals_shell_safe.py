"""Static-lint guard for shell-metachar injection in `termDraftLaunchCommand`.

The function in `.ai/dashboard/app/terminals.js` builds a string that is
sent into a running PTY via WebSocket (`ws.send(enc.encode(payload))`).
Any character outside the strict allowlist would be evaluated by the
shell — `;`, `$()`, backtick, `&&`, newlines, spaces — turning a
dropdown value into arbitrary command execution. The dropdown is
populated from `MODELS_BY_TOOL`, which can be mutated from devtools or
poisoned by a future cache write, so the safety must live at the build
site, not at the input site.

Static-lint pattern over JS source (per project convention; no jsdom
available — see `tests/test_jobs_static_refactor.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TERMINALS_JS = REPO_ROOT / ".ai" / "dashboard" / "app" / "terminals.js"


def _extract_function_body(src: str, fn_name: str) -> str:
    """Return the body (including signature) of a top-level function
    declared as `function <fn_name>(...) { ... }` inside src.
    Uses brace counting; assumes the function is syntactically clean.
    """
    pattern = re.compile(rf"function\s+{re.escape(fn_name)}\s*\([^)]*\)\s*\{{")
    m = pattern.search(src)
    if not m:
        raise AssertionError(f"function {fn_name} not found in terminals.js")
    start = m.start()
    i = m.end() - 1  # at the opening `{`
    depth = 1
    i += 1
    while i < len(src) and depth > 0:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return src[start:i]


def test_term_draft_launch_command_uses_strict_allowlist():
    """`safeModel` MUST be derived via a `[A-Za-z0-9._-]+`-shaped
    allowlist test, not a simple character strip. Quote-stripping
    (the previous implementation) does not defeat `;`, `$()`,
    backtick, newline, or space — the OWASP rule for building command
    strings is allowlist, never blocklist.
    """
    src = TERMINALS_JS.read_text(encoding="utf-8")
    body = _extract_function_body(src, "termDraftLaunchCommand")

    # Allowlist regex must appear in the function body.
    assert re.search(
        r"/\^\[A-Za-z0-9\._\-\]\+\$/", body
    ) or re.search(
        r"/\^\[A-Za-z0-9\._-\]\+\$/", body
    ), (
        "termDraftLaunchCommand must validate model against the strict "
        "allowlist /^[A-Za-z0-9._-]+$/ before interpolating into the "
        "shell command. Quote-stripping is insufficient against shell "
        "metachars."
    )

    # The .test() call on the allowlist must gate safeModel assignment.
    assert re.search(r"safeModel\s*=\s*/\^\[A-Za-z0-9\._\-?\]\+\$/\.test\(", body), (
        "safeModel must be assigned via the allowlist `.test()` result "
        "(ternary or equivalent), not via a simple .replace() strip."
    )


def test_term_draft_launch_command_no_quote_strip_only():
    """Regression guard: the bare `.replace(/"/g, "")` pattern as the
    SOLE sanitization step must not return. It is a blocklist on a
    single character (`"`); shell metachars survive intact.
    """
    src = TERMINALS_JS.read_text(encoding="utf-8")
    body = _extract_function_body(src, "termDraftLaunchCommand")

    # If a .replace(/"/g, "") appears, it must coexist with the allowlist
    # regex (defense-in-depth is fine; replace-as-only-defense is not).
    if re.search(r'\.replace\(\s*/\"/g\s*,\s*""\s*\)', body):
        assert re.search(r"/\^\[A-Za-z0-9\._\-\]\+\$/", body), (
            "Found .replace(/\"/g, '') without the allowlist regex "
            "guarding safeModel. That's a blocklist on one character "
            "while shell metachars (; $() backtick && newline space) "
            "pass through to the PTY."
        )


def test_term_draft_launch_command_rejects_metachar_models():
    """Symbolic check: re-run the JS regex on a panel of attack
    strings (using Python's regex with the same charset) and confirm
    each is rejected. This exercises the policy, not the JS engine.
    """
    allowlist = re.compile(r"^[A-Za-z0-9._-]+$")
    attacks = [
        "claude-sonnet; rm -rf ~",
        "gpt-5.5$(touch /tmp/pwned)",
        "claude-`whoami`",
        "model && evil",
        "model\nwhoami",
        "model with space",
        "claude\"; echo pwned",
    ]
    for s in attacks:
        assert not allowlist.match(s), f"allowlist must reject: {s!r}"

    legit = [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "claude-haiku-4-5",
    ]
    for s in legit:
        assert allowlist.match(s), f"allowlist must accept legit model: {s!r}"
