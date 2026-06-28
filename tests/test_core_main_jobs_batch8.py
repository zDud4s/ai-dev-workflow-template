"""Batch 8 (final) — residual LOW + PERF sweep for core.js / main.js / jobs.js.

Per docs/bug-hunt-status.md the remaining open LOW items after batch 7 were
sleeper polish: magic-number literals scattered across the file, a dead
runtime branch in `_eventsState.expanded` init, and a couple of unwired
animation/timeout constants.  PERF for main.js (5 fetches serial -> Promise.all)
was already landed in batch 2 and is re-pinned here as a regression guard.

Pins applied:

core.js
  - TOAST_DISMISS_MS_OK / WARN / ERR named constants replace the inline
    `3500 / 4500 / 6000` magic numbers in showToast.
  - TOAST_EXIT_ANIM_MS named constant replaces the inline `220` in hideToast.

main.js
  - PERF: Promise.all([...7 fetches]) inside loadAll — guard that the
    serial-await regression doesn't sneak back in.
  - BANNER_EXIT_ANIM_MS named constant replaces the inline `220` in
    dismissUpdateBanner.

jobs.js
  - JOB_TASK_PREVIEW_LEN replaces the two inline `80` literals in the row
    template (slice + length-overflow ellipsis must use the same constant).
  - JOB_LOG_TAIL_LINES replaces the inline `?tail=400` query string AND the
    "log (last 400 lines)" caption so they always agree.
  - JOB_LOG_BOTTOM_SLOP_PX replaces the inline `< 50` scroll-bottom slop.
  - VISIBILITY_DEDUPE_MS replaces the inline `< 250` debounce gate.
  - EVENTS_AUTOREFRESH_MS replaces both inline `5000` setInterval calls.
  - Dead branch removed: `_eventsState.expanded` initialised directly to
    `new Set()` (the previous `typeof === "object" || === null` check was
    always-true given the `null` literal on the line above).
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Brace-balanced body of `[async] function NAME(...)`."""
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


# ===== core.js =====================================================


def test_core_toast_dismiss_constants_declared() -> None:
    """Inline `3500 / 4500 / 6000` magic numbers in the toast dismiss
    ternary make it impossible to grep for "toast timeout" without
    chasing literals.  Surface them as named constants."""
    src = _src("core.js")
    for name, val in (
        ("TOAST_DISMISS_MS_OK", "3500"),
        ("TOAST_DISMISS_MS_WARN", "4500"),
        ("TOAST_DISMISS_MS_ERR", "6000"),
    ):
        assert re.search(
            r"\bvar\s+" + name + r"\s*=\s*" + val + r"\s*;",
            src,
        ), (
            "core.js must declare `var " + name + " = " + val + ";`"
            " — the inline magic number must be lifted to a named constant"
        )


def test_core_toast_dismiss_ternary_uses_constants() -> None:
    """The dismiss-cadence ternary in showToast must reference the
    constants — not the raw literals it used to embed."""
    body = _function_body(_src("core.js"), "showToast")
    # No raw magic literals in the ternary line.
    ternary_block = re.search(
        r"const\s+dismissAfter\s*=\s*\(timeoutMs\s*!=\s*null\)([\s\S]+?);",
        body,
    )
    assert ternary_block is not None, (
        "showToast must still have the `const dismissAfter = …` ternary"
    )
    block = ternary_block.group(1)
    assert "3500" not in block, "Inline 3500 must move to TOAST_DISMISS_MS_OK"
    assert "4500" not in block, "Inline 4500 must move to TOAST_DISMISS_MS_WARN"
    assert "6000" not in block, "Inline 6000 must move to TOAST_DISMISS_MS_ERR"
    for name in (
        "TOAST_DISMISS_MS_ERR",
        "TOAST_DISMISS_MS_WARN",
        "TOAST_DISMISS_MS_OK",
    ):
        assert name in block, "Ternary must reference " + name


def test_core_toast_exit_anim_constant_declared_and_used() -> None:
    """The 220ms exit-animation in hideToast must use a named constant
    that matches the CSS `.toast.out` transition.  Pinning the constant
    means a future CSS tweak can `grep TOAST_EXIT_ANIM_MS` for the JS
    side of the contract."""
    src = _src("core.js")
    assert re.search(
        r"\bvar\s+TOAST_EXIT_ANIM_MS\s*=\s*220\s*;",
        src,
    ), "core.js must declare `var TOAST_EXIT_ANIM_MS = 220;`"
    body = _function_body(src, "hideToast")
    # The setTimeout must use the constant, not the literal.
    assert re.search(
        r"setTimeout\([^,]+,\s*TOAST_EXIT_ANIM_MS\s*\)",
        body,
    ), "hideToast setTimeout must reference TOAST_EXIT_ANIM_MS"
    assert ", 220)" not in body, (
        "hideToast must no longer hold the inline 220 literal"
    )


# ===== main.js =====================================================


def test_main_load_all_uses_promise_all_for_initial_fetches() -> None:
    """PERF regression guard — batch 2 collapsed the 5 sequential
    `await fetch(...)` calls into a single Promise.all.  A future refactor
    must not re-introduce the cold-boot latency cliff."""
    body = _function_body(_src("main.js"), "loadAll")
    assert "Promise.all(" in body, (
        "loadAll must batch initial fetches with Promise.all to avoid the "
        "5x serial-await cold-boot latency that batch 2 fixed"
    )
    # And the destructured array of resolved values must include all the
    # key sources — project, models, memory, decisions, plans, specs, packets.
    promise_all = re.search(
        r"Promise\.all\(\s*\[([\s\S]+?)\]\s*\)",
        body,
    )
    assert promise_all is not None
    payload = promise_all.group(1)
    for required in (
        '"\\.ai/project.yaml"',
        '"\\.ai/models.yaml"',
        '"\\.ai/memory.md"',
        '"\\.ai/decisions.md"',
        '"\\.ai/plans"',
        '"\\.ai/specs"',
        '"\\.ai/packets"',
    ):
        assert re.search(required, payload), (
            "Promise.all batch must still include " + required
            + " — losing any of them re-serialises the cold boot"
        )


def test_main_banner_exit_anim_constant() -> None:
    """The dismissUpdateBanner inline `220` must use a named constant so
    the JS side of the CSS `.update-banner.out` transition is explicit."""
    src = _src("main.js")
    assert re.search(
        r"\bvar\s+BANNER_EXIT_ANIM_MS\s*=\s*220\s*;",
        src,
    ), "main.js must declare `var BANNER_EXIT_ANIM_MS = 220;`"
    body = _function_body(src, "dismissUpdateBanner")
    assert "BANNER_EXIT_ANIM_MS" in body, (
        "dismissUpdateBanner must reference BANNER_EXIT_ANIM_MS"
    )
    assert ", 220)" not in body, (
        "dismissUpdateBanner must no longer carry an inline 220 literal"
    )


# ===== jobs.js =====================================================


def test_jobs_task_preview_len_constant() -> None:
    """The two inline `80` literals in the row template (slice + ellipsis
    threshold) must use the same constant so they can't drift."""
    src = _src("jobs.js")
    assert re.search(
        r"\bvar\s+JOB_TASK_PREVIEW_LEN\s*=\s*80\s*;",
        src,
    ), "jobs.js must declare `var JOB_TASK_PREVIEW_LEN = 80;`"
    body = _function_body(src, "loadJobs")
    assert "taskPreview.slice(0, 80)" not in body, (
        "loadJobs row template must use JOB_TASK_PREVIEW_LEN, not `80`"
    )
    # And the new shape: slice + length compare both reference the const.
    assert "taskPreview.slice(0, JOB_TASK_PREVIEW_LEN)" in body
    assert "taskPreview.length > JOB_TASK_PREVIEW_LEN" in body


def test_jobs_log_tail_lines_constant() -> None:
    """The `?tail=400` query string and the "log (last 400 lines)" caption
    must use the same constant so they can never disagree."""
    src = _src("jobs.js")
    assert re.search(
        r"\bvar\s+JOB_LOG_TAIL_LINES\s*=\s*400\s*;",
        src,
    ), "jobs.js must declare `var JOB_LOG_TAIL_LINES = 400;`"
    body = _function_body(src, "loadJobDetail")
    # No raw `?tail=400`.
    assert "?tail=400" not in body, (
        "loadJobDetail must build the tail query from JOB_LOG_TAIL_LINES"
    )
    assert "?tail=" in body and "JOB_LOG_TAIL_LINES" in body, (
        "loadJobDetail must concat the tail query with JOB_LOG_TAIL_LINES"
    )
    # The visible caption must echo the same constant via template literal.
    assert "log (last 400 lines)" not in body, (
        "loadJobDetail caption must use the JOB_LOG_TAIL_LINES interp, "
        "not the hard-coded `last 400 lines` string"
    )
    assert "${JOB_LOG_TAIL_LINES}" in body


def test_jobs_log_bottom_slop_constant() -> None:
    """`< 50` slop for "log scrolled to bottom?" check — surface as a
    named constant so the heuristic is greppable."""
    src = _src("jobs.js")
    assert re.search(
        r"\bvar\s+JOB_LOG_BOTTOM_SLOP_PX\s*=\s*50\s*;",
        src,
    ), "jobs.js must declare `var JOB_LOG_BOTTOM_SLOP_PX = 50;`"
    body = _function_body(src, "loadJobDetail")
    assert " < 50)" not in body, (
        "loadJobDetail wasAtBottom heuristic must use the named constant"
    )
    assert "JOB_LOG_BOTTOM_SLOP_PX" in body


def test_jobs_visibility_dedupe_constant() -> None:
    """`< 250` debounce window for the visibilitychange double-fire guard
    must be a named constant (matches the batch-5 dedupe behaviour and
    makes the rationale greppable)."""
    src = _src("jobs.js")
    assert re.search(
        r"\bvar\s+VISIBILITY_DEDUPE_MS\s*=\s*250\s*;",
        src,
    ), "jobs.js must declare `var VISIBILITY_DEDUPE_MS = 250;`"
    # And the inline 250 inside the listener must be gone.
    handler = re.search(
        r"addEventListener\(\s*\"visibilitychange\"[\s\S]+?\}\);",
        src,
    )
    assert handler is not None, (
        "jobs.js must still register the visibilitychange listener"
    )
    block = handler.group(0)
    assert " < 250)" not in block, (
        "Inline `< 250)` literal in the visibility debounce must be gone"
    )
    assert "VISIBILITY_DEDUPE_MS" in block


def test_jobs_events_autorefresh_constant() -> None:
    """The two `setInterval(loadEvents, 5000)` call sites must share a
    single named constant — they currently power the same auto-refresh
    UX from two entry points (manual checkbox toggle + visibilitychange)."""
    src = _src("jobs.js")
    assert re.search(
        r"\bvar\s+EVENTS_AUTOREFRESH_MS\s*=\s*5000\s*;",
        src,
    ), "jobs.js must declare `var EVENTS_AUTOREFRESH_MS = 5000;`"
    # Neither call site retains the literal.
    matches = re.findall(r"setInterval\(\s*loadEvents\s*,\s*\d+\s*\)", src)
    assert not matches, (
        "Both setInterval(loadEvents, ...) sites must reference "
        "EVENTS_AUTOREFRESH_MS, not the inline 5000 literal — found: "
        + repr(matches)
    )
    # And the constant must drive both call sites. dash-perf wrapped the bare
    # `setInterval(loadEvents, ...)` in a visibility gate (only refresh while
    # the events view is active), so accept either the bare form or the gated
    # wrapper — both call loadEvents on the EVENTS_AUTOREFRESH_MS cadence.
    const_calls = re.findall(
        r"setInterval\(\s*(?:loadEvents\s*,|function[\s\S]{0,200}?loadEvents\(\)[\s\S]{0,80}?,)\s*EVENTS_AUTOREFRESH_MS\s*\)",
        src,
    )
    assert len(const_calls) >= 2, (
        "Both setInterval(...loadEvents...) sites must use EVENTS_AUTOREFRESH_MS"
    )


def test_jobs_events_state_expanded_init_is_set() -> None:
    """Dead branch removed: `_eventsState.expanded` previously initialised
    to `null` and then a `typeof === "object" || === null` block (always
    true) re-assigned a fresh Set.  Initialise directly to `new Set()`."""
    src = _src("jobs.js")
    assert re.search(
        r"_eventsState\s*=\s*\{[^}]*\bexpanded\s*:\s*new\s+Set\(\)",
        src,
    ), (
        "_eventsState.expanded must initialise directly to `new Set()` — "
        "the prior `: null` + always-true re-assign block was dead code"
    )
    # And the dead `if (typeof _eventsState.expanded === "object" ||` block
    # must be gone.
    assert "typeof _eventsState.expanded ===" not in src, (
        "The always-true typeof reassignment block must be removed"
    )


# ===== Regression guards for earlier-batch fixes that overlap this scope =====


def test_jobs_safe_tool_still_routed_through_pillTool() -> None:
    """Regression guard — _jobsSafeTool whitelist must still gate every
    pillTool() call site in jobs.js."""
    src = _src("jobs.js")
    pill_calls = re.findall(r"pillTool\(([^)]+)\)", src)
    assert pill_calls, "jobs.js must still call pillTool() somewhere"
    for arg in pill_calls:
        assert "_jobsSafeTool" in arg, (
            "pillTool() call site must route through _jobsSafeTool(): " + arg
        )


def test_jobs_visibility_debounce_still_uses_and_not_or() -> None:
    """The visibility debounce must use AND (`state-equal AND inside-window`)
    so the FIRST fire isn't blocked.  Constant rename must not flip the
    operator."""
    src = _src("jobs.js")
    m = re.search(
        r"_lastVisibilityState\s*===\s*state\s*(&&|\|\|)\s*\(\s*now\s*-",
        src,
    )
    assert m is not None, "Visibility debounce guard regex must still match"
    assert m.group(1) == "&&", (
        "Visibility debounce must use AND, not OR — flipping the operator "
        "would drop EVERY visibilitychange fire after the first one"
    )


def test_core_toasts_map_still_module_local() -> None:
    """Regression guard — TOASTS must remain a module-local Map.  A future
    refactor that promotes it to `window.TOASTS` would break the leak fix
    landed in batch 5."""
    src = _src("core.js")
    assert re.search(r"\bvar\s+TOASTS\s*=\s*new\s+Map\(\)", src), (
        "core.js must keep TOASTS as a module-local `var TOASTS = new Map()`"
    )
    assert "window.TOASTS" not in src, (
        "TOASTS must NOT be promoted to a window global"
    )
