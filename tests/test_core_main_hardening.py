"""Hardening tests for .ai/dashboard/app/core.js and main.js.

Covers:
  - Internal-helper null-guard rollout (at least 5 entry-point guards).
  - editPhaseRow / savePhaseRow event delegation (no inline onclick=).
  - Idempotent delegation flag on the models table.
  - AbortController + timeout on fetch helpers.
  - listDir non-2xx handling (warn-or-throw, not silent return []).
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name):
    return (APP / name).read_text(encoding="utf-8")


def test_core_has_at_least_5_entry_point_null_guards():
    """Functions that dereference DOM nodes should bail early if the canary
    element is missing. Existing 3 entry-points (renderOverview, renderActivity,
    renderModels) plus the new internal-helper guards should give >= 5."""
    src = _src("core.js")
    # Match `if (!$("#...")) return` (one-line guard at function tops).
    pattern = re.compile(r'if\s*\(\s*!\$\(["\'][^"\']+["\']\)\s*\)\s*return\b')
    matches = pattern.findall(src)
    assert len(matches) >= 5, (
        "core.js should have at least 5 `if (!$(...)) return` entry-point "
        "null-guards (found %d)" % len(matches)
    )


def test_models_table_uses_data_attrs_not_onclick():
    """The phase Edit / Save buttons used inline onclick="" handlers, which is
    a latent XSS pattern even though the phase tokens are currently hardcoded.
    They must use data-* attributes instead, driven by a delegated listener."""
    src = _src("core.js")
    assert 'onclick="editPhaseRow(' not in src, (
        "core.js should no longer emit inline onclick=\"editPhaseRow(...)\" "
        "markup — use data-edit-phase + delegated listener"
    )
    assert 'onclick="savePhaseRow(' not in src, (
        "core.js should no longer emit inline onclick=\"savePhaseRow(...)\" "
        "markup — use data-save-phase + delegated listener"
    )
    assert "data-edit-phase=" in src, (
        "core.js should emit data-edit-phase=\"...\" on the Edit button"
    )
    assert "data-save-phase=" in src, (
        "core.js should emit data-save-phase=\"...\" on the Save button"
    )


def test_models_table_has_delegated_listener():
    """The delegated click listener on #models-table must be wired exactly
    once — guarded by a module-level flag, the canonical pattern from
    jobs.js / settings.js."""
    src = _src("core.js")
    assert "_modelsTableDelegationWired" in src, (
        "core.js should declare a `_modelsTableDelegationWired` flag (or "
        "equivalent) so the delegated click listener is wired only once"
    )


def test_fetch_helpers_use_abort_controller():
    """`getText` and `listDir` must wrap fetch() with an AbortController +
    setTimeout so a hung server can't stall dashboard load forever."""
    src = _src("core.js")

    def _function_body(name):
        # Naively pull the first async function block by brace-counting.
        marker = "async function " + name + "("
        start = src.index(marker)
        brace_open = src.index("{", start)
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

    get_text_body = _function_body("getText")
    list_dir_body = _function_body("listDir")

    for name, body in (("getText", get_text_body), ("listDir", list_dir_body)):
        assert "AbortController" in body, (
            "%s should construct an AbortController so fetch() can be cancelled "
            "on timeout" % name
        )
        assert "signal:" in body or "signal :" in body, (
            "%s should pass `signal: ctrl.signal` to fetch() so the controller "
            "actually cancels the request" % name
        )


def test_listdir_handles_non_ok():
    """`listDir` previously swallowed non-2xx with `return []`, hiding server
    errors behind an "Empty" UI. Operators need to either see an exception or
    a console.warn — silent failure is not acceptable."""
    src = _src("core.js")
    marker = "async function listDir("
    start = src.index(marker)
    brace_open = src.index("{", start)
    depth = 0
    end = brace_open
    for i in range(brace_open, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    body = src[brace_open:end]
    assert ("throw" in body) or ("console.warn" in body), (
        "listDir should either throw on non-2xx or log via console.warn so "
        "operators can see HTTP errors — silent `return []` is not enough"
    )
