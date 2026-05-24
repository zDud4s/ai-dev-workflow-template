"""Batch 5 fixes for .ai/dashboard/app/jobs.js.

Static-lint assertions over the source so we catch regressions without
needing a JS runtime. Covers:

1. visibilitychange double-fire dedupe / debounce.
2. clearTimeout race after await -- _jobsTimer cleared before the
   `wasPending` immediate-retry setTimeout, so we never have two
   pending loadJobs schedules at once.
3. relativeTime guards against negative or NaN diff (future-dated
   ISO strings or `new Date("garbage")`) so we don't render "-3s ago"
   and don't mis-bucket negatives into the largest unit.
4. Polling fallback at 15s for an active Run tab with no running
   jobs is still present (regression guard for batch-3 fix).
"""

from __future__ import annotations

import re
from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "jobs.js"


def _src() -> str:
    return JOBS_JS.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Brace-balanced body of a top-level `function NAME(...)`."""
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


# Fix 1 -------------------------------------------------------------------

def test_visibilitychange_listener_dedupes_double_fire():
    """Browsers can fire `visibilitychange` twice when a tab regains
    focus (Safari, older Chrome+bfcache). The handler must coalesce
    duplicate transitions inside a short window so loadJobs / loadEvents
    don't race themselves on focus."""
    src = _src()
    # Find the visibilitychange handler block.
    m = re.search(r'addEventListener\(\s*"visibilitychange"', src)
    assert m, "visibilitychange listener missing"
    # Slice ~3KB after the match -- enough to cover the entire arrow body.
    block = src[m.start():m.start() + 3000]
    # Some form of debounce / dedupe state must be tracked.
    has_state = bool(
        re.search(r'_lastVisibility(State|At)\b', block)
        or re.search(r'_visibilityDebounce', block)
        or re.search(r'_visTick', block)
    )
    assert has_state, (
        "visibilitychange handler must track last-state and/or timestamp "
        "to suppress duplicate fires (e.g. _lastVisibilityState + "
        "_lastVisibilityAt)"
    )
    # And it must compare against a time window (Date.now() delta).
    assert re.search(r'Date\.now\(\)', block), (
        "debounce must use Date.now() so duplicates inside a small window "
        "are dropped before we fan out to loadJobs / loadEvents"
    )


# Fix 2 -------------------------------------------------------------------

def test_clearTimeout_race_guard_in_pending_retry():
    """loadJobs sets _jobsTimer near the end of its happy path, then
    falls through to the `finally` block. If a concurrent caller flipped
    `_jobsLoadInFlight = "pending"` during the awaits, the finally
    block schedules a `setTimeout(loadJobs, 0)` retry. Without
    clearing _jobsTimer first, both the just-armed background poll
    AND the immediate retry are alive -- a double-armed timer race.
    """
    body = _function_body(_src(), "loadJobs")
    # Locate the wasPending branch.
    m = re.search(r'wasPending', body)
    assert m, "loadJobs finally block must check `wasPending`"
    # Slice from `wasPending` to end-of-function -- we expect the
    # cleanup + setTimeout retry inside this region.
    tail = body[m.start():]
    # Must clear _jobsTimer BEFORE scheduling the retry.
    clear_pos = tail.find("clearTimeout(_jobsTimer)")
    set_pos = tail.find("setTimeout(loadJobs, 0)")
    assert clear_pos != -1, (
        "wasPending branch must clearTimeout(_jobsTimer) so the just-armed "
        "background poll doesn't race the immediate retry"
    )
    assert set_pos != -1, (
        "wasPending branch must still re-trigger via setTimeout(loadJobs, 0)"
    )
    assert clear_pos < set_pos, (
        "clearTimeout(_jobsTimer) must run BEFORE setTimeout(loadJobs, 0) "
        "in the wasPending branch -- otherwise both timers fire"
    )


# Fix 3 -------------------------------------------------------------------

def test_relativeTime_guards_negative_and_nan():
    """`relativeTime(iso)` must produce sane output when `iso` is in
    the future (clock skew) or unparseable. The fix is a Math.max(0, ...)
    + Number.isFinite check so we don't render "-3s ago" or fall into
    the "d ago" bucket because of a negative diff."""
    body = _function_body(_src(), "relativeTime")
    # Guard with Math.max(0, ...).
    assert re.search(r'Math\.max\(\s*0\s*,', body), (
        "relativeTime must clamp diff with Math.max(0, ...) so future-"
        "dated timestamps don't render as negative ('-3s ago')"
    )
    # And handle NaN explicitly -- either Number.isFinite or isNaN.
    assert re.search(r'Number\.isFinite\b', body) or re.search(r'isNaN\s*\(', body), (
        "relativeTime must guard NaN (from new Date('garbage')) so the "
        "bucket cascade doesn't compare against NaN, which is always false"
    )


# Fix 4 -------------------------------------------------------------------

def test_run_tab_fallback_poll_still_active():
    """Regression guard for batch-3 fix: when the Run tab is active
    and no jobs are running, loadJobs must still arm a slow background
    poll (>=10s) so externally-started jobs appear without a manual
    reload."""
    body = _function_body(_src(), "loadJobs")
    # The `else if (runTabActive)` branch must exist.
    assert re.search(r"else\s+if\s*\(\s*runTabActive\s*\)", body), (
        "loadJobs must keep the `else if (runTabActive)` fallback so "
        "polling continues when nothing is running"
    )
    # And it must schedule setTimeout >= 10000ms.
    timer_calls = re.findall(r"setTimeout\(\s*loadJobs\s*,\s*(\d+)\s*\)", body)
    delays = [int(d) for d in timer_calls]
    assert any(d >= 10_000 for d in delays), (
        f"slow background poll (>=10000ms) for Run tab fallback missing; "
        f"saw delays={delays}"
    )


# Bonus: the debounce shouldn't block the very first visibilitychange fire.
def test_visibilitychange_debounce_does_not_block_first_fire():
    """The debounce must use an OR (state-equal AND inside-window) so
    the FIRST fire (or the first transition after a long pause) is
    always handled. Static check: the guard reads `&&` not `||`.
    """
    src = _src()
    m = re.search(r'addEventListener\(\s*"visibilitychange"', src)
    assert m
    block = src[m.start():m.start() + 3000]
    # Look for the dedupe predicate: it should look like
    #   if (_lastVisibilityState === state && (now - _lastVisibilityAt) < <window>)
    # i.e. require BOTH "same state" AND "inside window" to skip.
    has_and_guard = bool(
        re.search(
            r'_lastVisibilityState\s*===\s*state\s*&&',
            block,
        )
    )
    assert has_and_guard, (
        "visibilitychange dedupe must require BOTH same-state AND inside-"
        "window to skip -- using only the timestamp check would drop the "
        "first transition on page load"
    )
