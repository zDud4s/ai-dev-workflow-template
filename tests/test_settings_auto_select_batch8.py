"""Static-lint regression tests for settings.js + auto-select.js batch-8 fixes.

This is the FINAL batch closing the remaining open MEDIUM + LOW items from
docs/bug-hunt-status.md after batches 1-7. Tests are pure-static regex /
DOM-less assertions over the JS source.

Items closed by this batch:

  1. settings.js workflowCheck dual-source-of-truth — the inline
     `$q("#btn-workflow-update").disabled = !data.has_updates` write at
     line 494 has been replaced by a single `renderWorkflowButtons()`
     call so the button's enabled state derives from _workflowState
     (the centralized state object).

  2. auto-select.js loadAutoSelect — both `meta.innerHTML` (line 163)
     and `meta.textContent` (line 175) writes have been guarded with
     `if (meta)`. The previous code's null-guard at line 132 only
     bailed out when `root` was missing; if `#auto-select-meta` was
     absent while `#auto-select-rankings` existed, the function would
     crash mid-load.

Plus regression pins for the items already fixed in prior batches:

  - settings.js phases CSV substring coerced via Array.isArray (batch 2)
  - settings.js option case mismatch normalized via toLowerCase (batch 4)
  - settings.js p.model wrapped with escHtml (batch 2)
  - settings.js p.tool class injection sanitized via /[^a-z0-9_-]/gi
  - settings.js postJson surface JSON parse via console.warn (batch 3)
  - settings.js withBusy isConnected guard before restore (batch 5)
  - settings.js escHtml covers ' and " (batch 5)
  - settings.js _settingsVersion + _if_match echo (batch 5)
  - settings.js loadedOnce still functions as a meaningful guard

These pins protect against accidental reverts during future refactors.
"""
import re
from pathlib import Path


APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"
SETTINGS_JS = APP / "settings.js"
AUTO_SELECT_JS = APP / "auto-select.js"


def _settings():
    return SETTINGS_JS.read_text(encoding="utf-8")


def _auto():
    return AUTO_SELECT_JS.read_text(encoding="utf-8")


def _slice(src, fn_name):
    """Return the body substring of `function fn_name` (sync or async) up to
    the first balanced closing brace at the function's top level."""
    pat = re.compile(r"(?:async\s+)?function\s+" + re.escape(fn_name) + r"\s*\([^)]*\)\s*\{")
    m = pat.search(src)
    assert m, "expected function " + fn_name + " in source"
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


# =====================================================================
# BATCH 8 PRIMARY FIXES
# =====================================================================


# ---- Fix 1: workflowCheck no longer pokes #btn-workflow-update.disabled inline ----
def test_workflow_check_no_direct_disabled_write():
    """workflowCheck must not write `.disabled` directly to the update
    button — that bypasses the centralized renderWorkflowButtons and
    creates the dual-source-of-truth bug the _workflowState object was
    introduced to eliminate.
    """
    body = _slice(_settings(), "workflowCheck")
    # No `#btn-workflow-update`...`.disabled = ...` direct write.
    assert not re.search(
        r"#btn-workflow-update[^\n]*\.disabled\s*=",
        body,
    ), (
        "workflowCheck must not poke #btn-workflow-update.disabled "
        "directly; mutate _workflowState and call renderWorkflowButtons()"
    )


def test_workflow_check_calls_render_after_state_mutation():
    """workflowCheck must call renderWorkflowButtons() after mutating
    _workflowState so the visual update is immediate (synchronous).
    A purely-rely-on-the-finally pattern delays the user-visible state
    change by one tick of the busy spinner.
    """
    body = _slice(_settings(), "workflowCheck")
    # Find the assignment to _workflowState.hasUpdates and confirm a
    # renderWorkflowButtons() call follows it before the catch/finally.
    m = re.search(r"_workflowState\.hasUpdates\s*=", body)
    assert m, "workflowCheck must update _workflowState.hasUpdates"
    after = body[m.end():]
    # Bail at the catch block — only count renderWorkflowButtons calls
    # that happen in the success path.
    catch_idx = after.find("} catch")
    if catch_idx >= 0:
        after = after[:catch_idx]
    assert "renderWorkflowButtons(" in after, (
        "workflowCheck must call renderWorkflowButtons() after mutating "
        "_workflowState.hasUpdates so the disabled state updates immediately"
    )


def test_render_workflow_buttons_is_only_disable_writer_for_update_btn():
    """Across the whole module, `#btn-workflow-update` must only have its
    .disabled flipped by renderWorkflowButtons (which writes through `p`).
    Any direct `$q("#btn-workflow-update").disabled = ...` write at top
    level is a SoT violation.
    """
    src = _settings()
    direct_writes = re.findall(
        r'\$q\(\s*"#btn-workflow-update"\s*\)\.disabled\s*=',
        src,
    )
    assert len(direct_writes) == 0, (
        f"Found {len(direct_writes)} direct $q(\"#btn-workflow-update\")"
        ".disabled assignments. All disabled mutations for this button "
        "must go through renderWorkflowButtons() (single source of truth)."
    )


# ---- Fix 2: auto-select.js meta null-guard ----
def test_auto_select_meta_innerHTML_null_guarded():
    """loadAutoSelect must guard `meta` before assigning innerHTML.
    The function's early-return only bails on `root` (line 132); if
    `#auto-select-meta` is absent while `#auto-select-rankings` exists,
    the unguarded write throws.
    """
    body = _slice(_auto(), "loadAutoSelect")
    # Locate the innerHTML assignment.
    m = re.search(r"meta\.innerHTML\s*=", body)
    assert m, "loadAutoSelect must still set meta.innerHTML"
    # A guard like `if (meta) meta.innerHTML = ...` or `meta && (meta.innerHTML = ...)`
    # must immediately precede the write.
    pre = body[max(0, m.start() - 30): m.start()]
    has_guard = (
        "if (meta)" in pre
        or "meta && " in pre
        or "meta?." in body  # optional chaining variant
    )
    # Accept the optional-chaining variant as well (meta?.innerHTML = ...).
    has_optional_chain = "meta?.innerHTML" in body
    assert has_guard or has_optional_chain, (
        "meta.innerHTML must be null-guarded — either `if (meta)` "
        "preceding the write, or `meta?.innerHTML = ...` optional chaining"
    )


def test_auto_select_meta_textContent_null_guarded():
    """The catch branch's `meta.textContent = "load failed"` must also be
    null-guarded. A failed fetch is the worst time to throw a second
    error on a missing DOM node.
    """
    body = _slice(_auto(), "loadAutoSelect")
    m = re.search(r"meta\.textContent\s*=", body)
    assert m, "loadAutoSelect must still set meta.textContent in catch"
    pre = body[max(0, m.start() - 30): m.start()]
    has_guard = (
        "if (meta)" in pre
        or "meta && " in pre
    )
    has_optional_chain = "meta?.textContent" in body
    assert has_guard or has_optional_chain, (
        "meta.textContent must be null-guarded — `if (meta)` or "
        "`meta?.textContent = ...`"
    )


def test_auto_select_meta_writes_consistent_with_guard():
    """All `meta.*` writes inside loadAutoSelect must be guarded — not
    just one of them. Half-guarding is worse than not guarding (gives a
    false sense of safety).
    """
    body = _slice(_auto(), "loadAutoSelect")
    writes = list(re.finditer(r"meta\.(innerHTML|textContent)\s*=", body))
    assert len(writes) >= 2, (
        "expected at least 2 meta.* writes (success path innerHTML + "
        "catch path textContent) in loadAutoSelect"
    )
    for w in writes:
        pre = body[max(0, w.start() - 30): w.start()]
        body_view = body  # check optional-chain across whole body for the same prop
        prop = body[w.start():w.end()].split(".")[1].split("=")[0].strip()
        has_guard = "if (meta)" in pre or "meta && " in pre
        has_optional_chain = ("meta?." + prop) in body_view
        assert has_guard or has_optional_chain, (
            f"meta.{prop} write at offset {w.start()} is unguarded; "
            "every meta.* write inside loadAutoSelect must check meta first"
        )


# =====================================================================
# REGRESSION PINS (items already fixed in prior batches)
# =====================================================================


# ---- MEDIUM: phases CSV substring match (batch 2) ----
def test_pin_phases_array_coercion():
    """If the server returned phases as a CSV string ("execute,review"),
    `.indexOf` on the plain string would substring-match ("reviewer"
    ticks "review"). fillAutoSelect must coerce via Array.isArray + split.
    """
    body = _slice(_settings(), "fillAutoSelect")
    assert "Array.isArray(cfg.phases)" in body, (
        "fillAutoSelect must coerce cfg.phases via Array.isArray to avoid "
        "substring matching on CSV strings"
    )
    assert 'typeof cfg.phases === "string"' in body, (
        "fillAutoSelect must explicitly handle the string-CSV branch"
    )
    assert "cfg.phases.split(" in body, (
        "the string branch must split on ',' (not assume an array)"
    )


# ---- MEDIUM: option case mismatch (batch 4) ----
def test_pin_reasoning_effort_case_insensitive():
    """A YAML with "Low"/"HIGH" must still select the right dropdown
    option — the comparison must be case-insensitive.
    """
    body = _slice(_settings(), "renderPhasesTable")
    has_norm_source = bool(re.search(
        r"current\s*=\s*[^;\n]*toLowerCase\(\)", body
    ))
    has_norm_compare = ("current.toLowerCase()" in body) or (
        "current === r.toLowerCase()" in body
    )
    assert has_norm_source or has_norm_compare, (
        "renderPhasesTable must compare reasoning_effort case-insensitively"
    )


# ---- MEDIUM: p.model rendered raw (batch 2) ----
def test_pin_p_model_escaped():
    """p.model must pass through escHtml — server-supplied strings could
    contain HTML special chars.
    """
    body = _slice(_settings(), "renderPhasesTable")
    assert "escHtml(p.model" in body, (
        "renderPhasesTable must escape p.model via escHtml()"
    )
    # And not interpolated raw via a `|| "?"` fallback alone.
    raw_pattern = re.search(r"\+\s*\(p\.model\s*\|\|", body)
    assert raw_pattern is None, (
        "p.model must not be concatenated raw without escHtml wrap"
    )


# ---- MEDIUM: p.tool class injection (batch 2) ----
def test_pin_p_tool_class_sanitized():
    """The ph-tool-<class> fragment must restrict to safe class chars."""
    body = _slice(_settings(), "renderPhasesTable")
    has_restrict = bool(re.search(
        r"replace\(\s*/\[\^a-z0-9_\-\]/gi\s*,\s*\"\"\s*\)",
        body,
    ))
    assert has_restrict, (
        "tool must be sanitized via /[^a-z0-9_-]/gi.replace before being "
        "interpolated into the ph-tool- class attribute"
    )


# ---- MEDIUM: postJson silent JSON parse (batch 3) ----
def test_pin_post_json_surfaces_parse_error():
    """postJson must log JSON parse failures via console.warn (or .error)
    instead of silently swallowing them.
    """
    body = _slice(_settings(), "postJson")
    assert "console.warn" in body or "console.error" in body, (
        "postJson must surface JSON parse failures via console.warn/error"
    )
    assert "/* ignore */" not in body, (
        "postJson must not silently ignore JSON parse failures"
    )


# ---- MEDIUM: withBusy detached node (batch 5) ----
def test_pin_with_busy_isConnected_guard():
    """withBusy's finally must check btn.isConnected before restoring
    state — re-render may have replaced the node.
    """
    body = _slice(_settings(), "withBusy")
    assert "isConnected" in body, (
        "withBusy must guard btn.isConnected in its finally block"
    )
    restore_idx = body.find("disabled = false")
    assert restore_idx >= 0, "withBusy must restore disabled to false"
    isconn_idx = body.find("isConnected")
    assert isconn_idx >= 0 and isconn_idx < restore_idx, (
        "isConnected guard must precede the disabled=false restore"
    )


# ---- MEDIUM: escHtml missing '/" (batch 5) ----
def test_pin_escHtml_covers_all_five_chars():
    """escHtml must cover <, >, &, ', " — the full OWASP-recommended set."""
    src = _settings()
    # The character class must include all five.
    has_class = bool(re.search(r"replace\(\s*/\[[<>&'\"]+\]/g", src))
    assert has_class, (
        "escHtml regex must include all of <, >, &, ', \""
    )
    # And the mapping must cover the entity refs.
    assert "&#39;" in src or "&apos;" in src, "missing single-quote mapping"
    assert "&quot;" in src, "missing double-quote mapping"


# ---- MEDIUM: workflowCheck dual-source consolidation (batch 5 + batch 8) ----
def test_pin_workflow_state_object():
    """_workflowState object must exist with the three derived flags."""
    src = _settings()
    assert "_workflowState" in src, (
        "settings.js must declare _workflowState as the SoT for the "
        "workflow check/update widget"
    )
    # The render function must be the single place reading state.
    assert re.search(r"function\s+renderWorkflowButtons\s*\(", src)
    body = _slice(src, "renderWorkflowButtons")
    for field in ("busy", "checked", "hasUpdates"):
        assert field in body, (
            f"renderWorkflowButtons must read _workflowState.{field}"
        )


# ---- MEDIUM: loadedOnce still meaningful ----
def test_pin_loadedOnce_used_as_init_guard():
    """loadedOnce must still gate the initial loadAllSettings call in
    initSettings — without it, an external `window.initSettings()` call
    would double-fetch on first activation.
    """
    src = _settings()
    body = _slice(src, "initSettings")
    assert "loadedOnce" in body, (
        "initSettings must check loadedOnce before calling loadAllSettings"
    )
    # And the success branch must set it.
    load_body = _slice(src, "loadAllSettings")
    assert "loadedOnce = true" in load_body, (
        "loadAllSettings must set loadedOnce=true on success"
    )


# ---- MEDIUM: lost-edit race guard via _settingsVersion (batch 5) ----
def test_pin_settings_version_lost_edit_guard():
    """All three save handlers must echo _if_match sourced from
    _settingsVersion so a future version-aware server can refuse stale writes.
    """
    src = _settings()
    for fn in ("saveImprover", "saveAutoSelect", "savePhaseRow"):
        body = _slice(src, fn)
        assert "_if_match" in body, (
            f"{fn} must attach _if_match to its POST body"
        )
        assert "_settingsVersion" in body, (
            f"{fn} must source _if_match from _settingsVersion"
        )


# ---- auto-select.js: ?? for numeric counters (batch 1/3) ----
def test_pin_auto_select_nullish_for_numerics():
    """data.samples / dropped / min_samples must use ?? (not ||) so a
    legitimate 0 is preserved instead of falling back.
    """
    body = _slice(_auto(), "loadAutoSelect")
    # All three counters use ??.
    for field in ("data.samples", "data.dropped_candidates", "data.min_samples"):
        # Pattern: <field> ?? <fallback>
        assert re.search(re.escape(field) + r"\s*\?\?", body), (
            f"{field} must use ?? (not ||) for null/undefined fallback"
        )


# ---- Post-Gemini-revert canonical state ----
def test_post_gemini_revert_state():
    """Per commit 9072946 (revert dashboard Gemini integration suspended),
    only isClaude + isCodex are defined in renderPhasesTable. The 'max'-
    only-for-claude rule still applies via !isClaude polarity (which now
    covers only codex). This batch must not touch this baseline.
    """
    body = _slice(_settings(), "renderPhasesTable")
    assert 'isClaude = tool === "claude"' in body, (
        "isClaude flag must remain in renderPhasesTable"
    )
    assert 'isCodex = tool === "codex"' in body, (
        "isCodex flag must remain in renderPhasesTable"
    )
    # Post-revert: there must be NO isGemini flag (suspended integration).
    assert "isGemini" not in body, (
        "isGemini was removed in revert 9072946; do not reintroduce it "
        "in batch 8"
    )
    assert "!isClaude" in body, (
        "max-only-for-claude rule must remain via !isClaude polarity"
    )
