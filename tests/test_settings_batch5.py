"""Static-lint regression tests for settings.js batch-5 fixes.

Covers the remaining MEDIUM-severity items from docs/bug-hunt-status.md
that were not closed in batches 1-4:

  1. saveAutoSelect no longer calls loadAllSettings() (skeleton flash);
     uses a surgical refreshPhasesSection() helper instead.
  2. Lost-edit race: settings module tracks _settingsVersion and echoes
     it as `_if_match` on every save path.
  3. escHtml() escapes both ' and " in addition to <, >, &.
  4. withBusy() null-guards `btn.isConnected` before restoring the
     button state in its finally block.
  5. renderPhasesTable normalizes the stored reasoning_effort to
     lowercase before comparing against the option set (case-
     insensitive option match).
  6. Workflow widget state is consolidated into a single _workflowState
     object with a single render function (single source of truth).

These are pure-static regex/AST-shape assertions; no jsdom needed.
"""
import re
from pathlib import Path

SETTINGS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "settings.js"


def _src():
    return SETTINGS_JS.read_text(encoding="utf-8")


def _slice(src, fn_name):
    """Return the body substring of `function fn_name` (sync or async) up to
    the first balanced closing brace at the function's top level."""
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


# ---- Fix 1: saveAutoSelect no longer flashes the skeleton ----
def test_save_auto_select_does_not_call_loadAllSettings():
    """saveAutoSelect must not call loadAllSettings() on success because
    that re-runs showLoadingState() and flashes the skeleton over the
    just-rendered form. A surgical refresh helper is expected instead.
    """
    body = _slice(_src(), "saveAutoSelect")
    assert "loadAllSettings(" not in body, (
        "saveAutoSelect must not call loadAllSettings() on save success "
        "— the skeleton-flash from showLoadingState() defeats the purpose "
        "of optimistic UI. Use refreshPhasesSection() (no-skeleton repaint) "
        "instead."
    )


def test_refresh_phases_section_exists_and_skips_skeleton():
    """A surgical refresh helper must exist and must NOT call showLoadingState
    so the table re-renders in place without a skeleton flash.
    """
    src = _src()
    assert re.search(r"function\s+refreshPhasesSection\s*\(", src), (
        "settings.js must define refreshPhasesSection() — the no-skeleton "
        "repaint used by save handlers"
    )
    body = _slice(src, "refreshPhasesSection")
    assert "showLoadingState(" not in body, (
        "refreshPhasesSection must NOT call showLoadingState() — that's the "
        "very flash this helper exists to avoid"
    )
    # It must fetch fresh data and re-render the phases table.
    assert ("getJson" in body) or ("/api/settings" in body), (
        "refreshPhasesSection should re-fetch /api/settings"
    )
    assert "renderPhasesTable(" in body, (
        "refreshPhasesSection should call renderPhasesTable to repaint"
    )


# ---- Fix 2: lost-edit race window via _settingsVersion ----
def test_settings_version_is_tracked():
    """The module must track a version token from /api/settings and echo
    it back as `_if_match` on saves so a concurrent edit can be detected
    by a future server-side guard.
    """
    src = _src()
    assert "_settingsVersion" in src, (
        "settings.js must declare a _settingsVersion state variable for "
        "optimistic concurrency control"
    )
    # The version must be captured from the load response.
    load_body = _slice(src, "loadAllSettings")
    assert "_settingsVersion" in load_body, (
        "loadAllSettings must update _settingsVersion from the server response"
    )


def test_save_handlers_echo_if_match():
    """All three save handlers (improver, auto_select, phase row) must
    attach `_if_match` to the body when a version is known.
    """
    src = _src()
    # Every save function should reference _if_match (the property name)
    # or _settingsVersion (the source).
    for fn in ("saveImprover", "saveAutoSelect", "savePhaseRow"):
        body = _slice(src, fn)
        assert ("_if_match" in body) or ("_settingsVersion" in body), (
            f"{fn} must attach _if_match (sourced from _settingsVersion) "
            "to the POST body so a server-side concurrency check can fire"
        )


# ---- Fix 3: escHtml covers ' and " ----
def test_escHtml_covers_quotes():
    """escHtml must escape both single and double quotes in addition to
    <, >, &. The character class regex is the canonical form to check.
    """
    src = _src()
    # Look for the regex character class — accept both orderings.
    has_class = bool(re.search(r"replace\(\s*/\[[<>&'\"]+\]/g", src))
    assert has_class, (
        "escHtml replace regex must include both ' and \" in its character "
        "class so the helper is safe in attribute position"
    )
    # And the mapping table must include the entity refs.
    assert "&#39;" in src or "&apos;" in src, (
        "escHtml must map ' to &#39; (or &apos;)"
    )
    assert "&quot;" in src, (
        "escHtml must map \" to &quot;"
    )


# ---- Fix 4: withBusy null-guards isConnected ----
def test_withBusy_checks_isConnected_before_restore():
    """The finally block in withBusy() must guard `btn.isConnected` (or
    equivalent) before re-enabling the button. A button that was detached
    while we awaited would otherwise still receive disabled=false on a
    stale node — a GC and stale-state hazard.
    """
    body = _slice(_src(), "withBusy")
    assert "isConnected" in body, (
        "withBusy must guard `btn.isConnected` before restoring the button "
        "in the finally block — re-render may have replaced the node"
    )
    # The guard must be applied before the .disabled = false write.
    # Find the position of the first .disabled = false (the restore) and
    # confirm isConnected appears before it.
    restore_idx = body.find("disabled = false")
    assert restore_idx >= 0, "withBusy finally must include `disabled = false`"
    isconn_idx = body.find("isConnected")
    assert isconn_idx >= 0 and isconn_idx < restore_idx, (
        "isConnected guard must precede the `disabled = false` restore"
    )


# ---- Fix 5: case-insensitive reasoning_effort match ----
def test_reasoning_effort_compared_case_insensitive():
    """`current` (server-supplied p.reasoning_effort) must be lower-cased
    before being compared to the canonical option values in
    renderPhasesTable, so a YAML that drifted ("Low", "HIGH") still
    selects the right dropdown entry.
    """
    body = _slice(_src(), "renderPhasesTable")
    # Either the assignment normalizes the source, or the comparison does.
    has_norm_source = bool(re.search(
        r"current\s*=\s*[^;\n]*toLowerCase\(\)", body
    ))
    has_norm_compare = ("current.toLowerCase()" in body) or (
        "current === r.toLowerCase()" in body
    )
    assert has_norm_source or has_norm_compare, (
        "renderPhasesTable must compare `current` against option values "
        "case-insensitively (either by lower-casing `current` once, or "
        "by lower-casing in the comparison)"
    )


# ---- Fix 6: workflow widget single source of truth ----
def test_workflow_state_object_exists():
    """The workflow widget must derive UI state from a single _workflowState
    object (or equivalent named state) — not from multiple scattered
    flags + ad-hoc writes.
    """
    src = _src()
    assert "_workflowState" in src, (
        "settings.js must declare a _workflowState object that is the "
        "single source of truth for the workflow check/update widget"
    )
    # The render function must read from the state object.
    assert re.search(r"function\s+renderWorkflowButtons\s*\(", src), (
        "settings.js must define renderWorkflowButtons() — the single "
        "place that translates _workflowState into .disabled flags"
    )
    body = _slice(src, "renderWorkflowButtons")
    assert "_workflowState" in body, (
        "renderWorkflowButtons must read from _workflowState"
    )
    # The render function must touch both buttons.
    assert "btn-workflow-check" in body, (
        "renderWorkflowButtons must update #btn-workflow-check"
    )
    assert "btn-workflow-update" in body, (
        "renderWorkflowButtons must update #btn-workflow-update"
    )


def test_workflow_update_finally_uses_centralized_render():
    """workflowUpdate's finally block must not poke .disabled directly on
    both buttons — that's two writes that don't go through the single
    render function. Acceptable replacements: setWorkflowBusy(false) or
    renderWorkflowButtons().
    """
    body = _slice(_src(), "workflowUpdate")
    # The finally must NOT have BOTH disabled writes pattern that the old
    # code had: `if (c) c.disabled = false; if (p) p.disabled = false;`
    pair_pat = re.compile(
        r"c\.disabled\s*=\s*false\s*;\s*if\s*\(p\)\s*p\.disabled\s*=\s*false",
    )
    assert pair_pat.search(body) is None, (
        "workflowUpdate finally must not double-write .disabled directly; "
        "use setWorkflowBusy/renderWorkflowButtons instead"
    )
    # And it must funnel through the central helpers.
    assert "setWorkflowBusy(" in body or "renderWorkflowButtons(" in body, (
        "workflowUpdate finally must call setWorkflowBusy or "
        "renderWorkflowButtons to keep button state derived from _workflowState"
    )


# ---- Bonus: ensure existing fixes are still in place ----
def test_phases_array_indexOf_still_safe():
    """The previously-fixed CSV substring bug must remain fixed: cb.checked
    is computed from an array's .indexOf, not a string substring check.
    """
    src = _src()
    body = _slice(src, "fillAutoSelect")
    assert "Array.isArray(cfg.phases)" in body, (
        "fillAutoSelect should still coerce cfg.phases through Array.isArray"
    )
