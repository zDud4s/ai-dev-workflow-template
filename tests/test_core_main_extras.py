"""Static-regex hardening tests for the remaining core.js / main.js items
from the bug-hunt status doc (agent 2, batch 5).

These cover gaps left by the prior batches:

  - renderOverview no longer double-calls `$()` for guard + assignment
    (single-lookup pattern eliminates the null-deref race the status doc
    flagged at core.js:198).
  - renderModels guards `#dispatch-cards` (previously only #dispatch-toggle
    and #models-table were guarded; an unguarded dataset access on
    dispatchCards aborted the rest of the function).
  - renderActivity sort comparator uses String() coercion so a non-string
    `name` value cannot blow up `.localeCompare`.
  - renderProject guards #project-boundaries and #project-raw (previously
    only #project-stack was guarded).
  - main.js `$("#meta").innerHTML = ...` is null-guarded so a stripped shell
    of index.html doesn't abort loadAll() before skeletons render.
  - main.js jsyaml.load no longer silently swallows malformed YAML; a
    parse exception is logged and surfaced to the user via setMsg toast.
  - TOASTS Map entry is removed up-front in hideToast so a racing replace
    or second hide cannot leave the channel keyed indefinitely.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name):
    return (APP / name).read_text(encoding="utf-8")


def _function_body(src, name):
    """Brace-count the body of `[async] function NAME(...)`."""
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


# ----- core.js -----

def test_renderOverview_single_lookup_no_double_dollar():
    """The previous shape was:
        if (!$("#overview-cards")) return;
        ...
        const overviewCards = $("#overview-cards");
        delete overviewCards.dataset.skeletoned;
    Two `$()` calls split by a DOM mutation could null-deref. Verify the
    function uses ONE lookup pattern (`const X = $(...); if (!X) return;`)."""
    src = _src("core.js")
    body = _function_body(src, "renderOverview")
    # Count `$("#overview-cards")` occurrences in the body.
    occurrences = len(re.findall(r'\$\("#overview-cards"\)', body))
    assert occurrences == 1, (
        "renderOverview should look up #overview-cards exactly once "
        "(found %d $() calls — split lookups risk null-deref between them)" % occurrences
    )
    # And the lookup result must be checked.
    assert re.search(
        r'const\s+overviewCards\s*=\s*\$\("#overview-cards"\)\s*;\s*if\s*\(\s*!\s*overviewCards\s*\)\s*return',
        body,
    ), "renderOverview should use single-lookup pattern with null-guard"


def test_renderModels_guards_dispatch_cards():
    """The previous guard checked only #dispatch-toggle and #models-table;
    a missing #dispatch-cards element fell through and threw on
    `dispatchCards.dataset.skeletoned`. Verify either an explicit guard or
    optional chaining on `dispatchCards`."""
    src = _src("core.js")
    body = _function_body(src, "renderModels")
    # Find the dispatchCards assignment.
    idx = body.index("dispatchCards")
    after = body[idx:]
    has_guard = (
        re.search(r"if\s*\(\s*dispatchCards\s*\)\s*\{", after) is not None
        or "dispatchCards?.dataset" in after
    )
    assert has_guard, (
        "renderModels must null-guard #dispatch-cards before dereferencing "
        "`.dataset` / `.innerHTML` — the existing guard only covered "
        "#dispatch-toggle and #models-table"
    )


def test_renderActivity_sort_coerces_to_string():
    """The sort comparator calls `.localeCompare` on `name`. If a future
    caller passes a non-string entry, the sort throws. Verify String()
    coercion at the comparator."""
    src = _src("core.js")
    body = _function_body(src, "renderActivity")
    assert re.search(r"String\(\s*[ab]\.name\s*\)\.localeCompare", body), (
        "renderActivity sort comparator should coerce `.name` to a String "
        "before calling .localeCompare to keep sorts stable on non-string "
        "inputs"
    )


def test_renderProject_guards_boundaries_and_raw():
    """Previously only #project-stack was guarded. #project-boundaries and
    #project-raw must each be wrapped in their own null-guard so a partial
    markup strip doesn't abort the rest of the function."""
    src = _src("core.js")
    body = _function_body(src, "renderProject")
    # Locate each lookup and verify a guard follows before .dataset.
    for var, sel in (
        ("boundaries", '$("#project-boundaries")'),
        ("raw", '$("#project-raw")'),
    ):
        assert sel in body, sel + " should still be looked up in renderProject"
        # Find the lookup line and confirm the next ~80 chars contain `if (var)`
        idx = body.index(sel)
        window = body[idx : idx + 200]
        assert re.search(r"if\s*\(\s*%s\s*\)" % re.escape(var), window), (
            "renderProject must null-guard `%s` before dereferencing `.dataset`" % var
        )


def test_hideToast_removes_map_entry_eagerly():
    """The Map.delete used to live inside the 220ms-delayed cleanup callback.
    Eager deletion ensures a second hideToast on the same channel — or a
    replacement showToast that races the exit animation — cannot leave the
    channel keyed indefinitely (memory leak)."""
    src = _src("core.js")
    body = _function_body(src, "hideToast")
    # Find the position of `TOASTS.delete(channel)` and verify it appears
    # BEFORE the setTimeout(...) call.
    del_idx = body.find("TOASTS.delete(channel)")
    settimeout_idx = body.find("setTimeout(")
    assert del_idx != -1, "hideToast must call TOASTS.delete(channel)"
    assert settimeout_idx != -1, "hideToast should still schedule DOM removal via setTimeout"
    assert del_idx < settimeout_idx, (
        "TOASTS.delete(channel) should run BEFORE setTimeout(...) — the "
        "previous shape deferred the delete by 220ms, leaving the Map "
        "entry alive during the exit animation"
    )


# ----- main.js -----

def test_main_meta_innerhtml_null_guarded():
    """`$("#meta").innerHTML = ...` at the top of loadAll() previously threw
    if the meta status element was missing. Verify the lookup is captured
    in a local and guarded before assignment."""
    src = _src("main.js")
    # No unguarded inline write.
    assert '$("#meta").innerHTML =' not in src, (
        "main.js should no longer have unguarded `$(\"#meta\").innerHTML = ...`"
    )
    # Verify the new shape: `const metaEl = $("#meta"); if (metaEl) metaEl.innerHTML ...`
    assert re.search(
        r'const\s+metaEl\s*=\s*\$\("#meta"\)\s*;\s*if\s*\(\s*metaEl\s*\)\s*metaEl\.innerHTML',
        src,
    ), (
        "main.js loadAll should declare `const metaEl = $(\"#meta\");` and "
        "null-guard every dereference"
    )


def test_main_meta_textcontent_null_guarded():
    """Both the success path (`updated TIME`) and the catch-block error path
    must null-guard the #meta write — the catch ran unguarded previously."""
    src = _src("main.js")
    # No unguarded `.textContent = ` write on $("#meta").
    assert '$("#meta").textContent' not in src, (
        "main.js should no longer have unguarded `$(\"#meta\").textContent`"
    )
    # Both branches must reference metaEl with a guard.
    assert re.search(
        r'if\s*\(\s*metaEl\s*\)\s*metaEl\.textContent\s*=\s*`updated',
        src,
    ), "Success path should guard metaEl before writing the timestamp"
    assert re.search(
        r'if\s*\(\s*metaEl\s*\)\s*metaEl\.textContent\s*=\s*"error"',
        src,
    ), "Error catch path should guard metaEl before writing 'error'"


def test_main_jsyaml_load_wrapped_in_try_catch():
    """`jsyaml.load(projectRaw) || {}` silently swallowed parse errors — a
    malformed project.yaml left the dashboard with a confusing empty UI
    and no signal to the operator. Verify both jsyaml.load calls live
    inside try/catch blocks that surface the failure."""
    src = _src("main.js")
    # The body of loadAll contains two `jsyaml.load(...)` calls; each must
    # be preceded by a `try {` within a short window.
    for var in ("projectRaw", "modelsRaw"):
        marker = "jsyaml.load(" + var + ")"
        assert marker in src, marker + " should still be present in main.js"
        idx = src.index(marker)
        preface = src[max(0, idx - 200) : idx]
        assert "try {" in preface or "try{" in preface, (
            marker + " must be wrapped in a try { ... } catch block — "
            "the previous `|| {}` only handled empty docs, not parse errors"
        )
    # And the catch handlers should surface the error (setMsg toast OR
    # console.error are both acceptable signals).
    catches = re.findall(
        r"catch\s*\(\s*e\s*\)\s*\{[^}]*?\}",
        src,
        flags=re.DOTALL,
    )
    relevant = [c for c in catches if "yaml" in c.lower()]
    assert relevant, (
        "Expected at least one catch block to surface a yaml parse error"
    )
    for c in relevant:
        assert ("setMsg(" in c) or ("console.error" in c) or ("console.warn" in c), (
            "yaml parse catch block should call setMsg / console.error / "
            "console.warn so the failure is not silent"
        )


def test_main_cards_null_guarded_in_catch():
    """The error catch in loadAll() did `cards.innerHTML = ...` and
    `delete cards.dataset.skeletoned` unguarded. Verify the catch now
    null-guards #overview-cards."""
    src = _src("main.js")
    # No unguarded write to cards.innerHTML at the top level of the catch.
    # The new shape wraps both ops inside `if (cards) { ... }`.
    assert re.search(
        r"const\s+cards\s*=\s*\$\("
        r'"#overview-cards"\)\s*;\s*if\s*\(\s*cards\s*\)\s*\{',
        src,
    ), (
        "main.js catch block must null-guard `cards` before writing innerHTML"
    )


def test_dec_decision_enter_submits():
    """#dec-decision should submit the decision form when Enter is pressed."""
    src = _src("core.js")
    assert re.search(
        r'\$\("#dec-decision"\)\?\.addEventListener\(\s*"keydown"\s*,\s*\([^)]*\)\s*=>\s*\{[^}]*e\.key\s*===\s*"Enter"[^}]*submitDecision\(',
        src,
        flags=re.DOTALL,
    ), (
        "#dec-decision needs a guarded keydown handler that calls submitDecision "
        "when Enter is pressed"
    )
