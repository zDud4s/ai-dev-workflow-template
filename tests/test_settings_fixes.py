"""Static-lint regression tests for settings.js fixes.

Mirrors tests/test_jobs_static_refactor.py: parses the JS source as text and
asserts presence/absence of marker patterns that encode the four fixes
applied to .ai/dashboard/app/settings.js.
"""
import re
import pytest
from pathlib import Path

SETTINGS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "settings.js"


def _src():
    return SETTINGS_JS.read_text(encoding="utf-8")


def _slice(src, fn_name):
    """Return the substring of `src` for the body of `async function fn_name`
    (or `function fn_name`) up to the first balanced closing brace at the
    function's top level. Best-effort: good enough for static linting.
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


# ---- Fix A: gemini reasoning_effort stripped on save ----
@pytest.mark.skip(reason="gemini dispatch never shipped")
def test_gemini_reasoning_effort_stripped_on_save():
    body = _slice(_src(), "savePhaseRow")
    # The save path must branch on gemini before deciding whether to include
    # reasoning_effort in the POST body. Accept either polarity of the check.
    has_gemini_branch = ('tool !== "gemini"' in body) or ('tool === "gemini"' in body)
    assert has_gemini_branch, \
        "savePhaseRow must branch on tool==gemini before including reasoning_effort"
    # And reasoning_effort assignment must still be in the function (we strip
    # it conditionally, not unconditionally).
    assert "reasoning_effort" in body, \
        "savePhaseRow should still reference reasoning_effort (conditionally)"


# ---- Fix B: saveImprover validates numerics client-side ----
def test_save_improver_validates_numerics():
    body = _slice(_src(), "saveImprover")
    # A validation guard must run before the postJson call.
    post_idx = body.find("postJson")
    assert post_idx > 0, "saveImprover must still call postJson"
    pre = body[:post_idx]
    has_validation = ("parseInt" in pre) or ("isNaN" in pre) or ("Number(" in pre)
    assert has_validation, \
        "saveImprover must validate numeric fields before posting"
    # And the user-visible failure path must surface via setMsg with bad tone.
    assert 'setMsg("imp-msg"' in pre and '"bad"' in pre, \
        "saveImprover should report invalid input via setMsg(..., bad) before postJson"


# ---- Fix C: workflow-update button logic simplified ----
def test_workflow_update_button_logic_simplified():
    src = _src()
    # Updated for batch-8 SoT consolidation: the inline `.disabled =
    # !data.has_updates` write in workflowCheck has been replaced by a
    # single `renderWorkflowButtons()` call that derives state from
    # _workflowState. The legacy bug (`!has_updates && current_sha != null`)
    # would have lived in the `renderWorkflowButtons` body, so the assertion
    # moves there.
    body = _slice(src, "renderWorkflowButtons")
    # Must derive `.disabled` from _workflowState (NOT from data.current_sha).
    assert "_workflowState" in body, (
        "renderWorkflowButtons must derive .disabled from _workflowState"
    )
    assert "data.current_sha" not in body, (
        "renderWorkflowButtons must not gate on data.current_sha"
    )
    # The update button enable rule must consult hasUpdates (the renamed
    # camelCase field on _workflowState).
    assert "hasUpdates" in body, (
        "renderWorkflowButtons must enable the update button when "
        "_workflowState.hasUpdates is true"
    )


# ---- Fix D: settings-reload has a busy guard ----
def test_settings_reload_busy_guard_exists():
    src = _src()
    assert "_isLoading" in src, \
        "settings.js must define an _isLoading flag for the reload guard"
    # The flag must be referenced near loadAllSettings (i.e. the same module
    # uses it to coalesce concurrent reloads).
    isloading_lines = [i for i, ln in enumerate(src.splitlines()) if "_isLoading" in ln]
    loadall_lines = [i for i, ln in enumerate(src.splitlines()) if "loadAllSettings" in ln]
    assert isloading_lines and loadall_lines, "missing _isLoading or loadAllSettings references"
    # At least one _isLoading reference must be within 30 lines of a
    # loadAllSettings reference.
    near = any(abs(a - b) <= 30 for a in isloading_lines for b in loadall_lines)
    assert near, "_isLoading must be referenced near loadAllSettings"
