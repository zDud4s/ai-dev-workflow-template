"""Medium-severity fixes for .ai/dashboard/app/jobs.js (batch 3).

Each test asserts a structural invariant in the source so we catch
regressions without needing a JS runtime. The four fixes are:

1. Run-tab background polling when no jobs are running.
2. loadSessions surfaces fetch errors instead of silently swallowing.
3. loadJobs clears _selectedJobId when the selected job is pruned.
4. submitJob avoids the loadJobDetail flash for chat-kind jobs.
"""

from __future__ import annotations

import re
from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "jobs.js"


def _src() -> str:
    return JOBS_JS.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Return the source of the named function (best-effort brace-match).

    Mirrors the helper in test_jobs_high_fixes.py so the two suites stay
    independently runnable.
    """
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

def test_polling_continues_on_run_tab_with_no_running_jobs():
    """When the Run tab is active and nothing is running, loadJobs must
    still schedule a slower background poll so externally-started jobs
    appear without a manual reload.

    We look for a setTimeout near a `runTabActive` branch with a delay of
    at least 10s (15s is the agreed cadence; any value >= 10_000 satisfies
    the "slower fallback" intent).
    """
    body = _function_body(_src(), "loadJobs")
    # There must be a branch that triggers when only the run tab is active.
    assert re.search(r"else\s+if\s*\(\s*runTabActive\s*\)", body), (
        "loadJobs must have an `else if (runTabActive)` branch so polling "
        "continues when no jobs are running but the Run tab is visible"
    )
    # And that branch (or one nearby) must schedule a setTimeout >= 10s.
    timer_calls = re.findall(r"setTimeout\(\s*loadJobs\s*,\s*(\d+)\s*\)", body)
    assert timer_calls, "loadJobs must schedule itself via setTimeout"
    delays = [int(d) for d in timer_calls]
    assert any(d >= 10_000 for d in delays), (
        f"expected a slow background poll (>=10000ms) for the run-tab "
        f"fallback; saw delays={delays}"
    )


# Fix 2 -------------------------------------------------------------------

def test_loadSessions_logs_errors():
    """loadSessions must not silently swallow fetch errors — it should
    log via console.warn so operators can diagnose stale dropdowns."""
    body = _function_body(_src(), "loadSessions")
    assert "console.warn" in body, (
        "loadSessions catch block must call console.warn (silent ignore "
        "was the bug — users saw stale options forever on network failure)"
    )
    # The literal "/* ignore */" sentinel from the old code must be gone.
    assert "/* ignore */" not in body, (
        "old `catch (_) { /* ignore */ }` swallow pattern still present in "
        "loadSessions — remove it so failures surface"
    )


# Fix 3 -------------------------------------------------------------------

def test_selected_job_cleared_when_pruned():
    """After fetching the jobs list, loadJobs must reset _selectedJobId if
    it no longer matches any returned job — otherwise loadJobDetail keeps
    hitting a dead id and renders "HTTP 404" indefinitely."""
    body = _function_body(_src(), "loadJobs")
    # Accept any of the common shapes for the membership check.
    patterns = [
        r"!\s*jobs\.find\(\s*\(?\s*j\s*\)?\s*=>\s*j\.id\s*===\s*_selectedJobId",
        r"jobs\.some\(\s*\(?\s*j\s*\)?\s*=>\s*j\.id\s*===\s*_selectedJobId",
        r"jobs\.findIndex\(\s*\(?\s*j\s*\)?\s*=>\s*j\.id\s*===\s*_selectedJobId",
    ]
    has_check = any(re.search(p, body) for p in patterns)
    assert has_check, (
        "loadJobs must verify _selectedJobId still exists in the freshly "
        "fetched jobs list (e.g. `if (!jobs.find(j => j.id === _selectedJobId))`)"
    )
    # And it must actually clear the id on the miss-path.
    assert re.search(r"_selectedJobId\s*=\s*null", body), (
        "loadJobs must assign `_selectedJobId = null` when the selected "
        "job is no longer in the list"
    )


# Fix 4 -------------------------------------------------------------------

def test_chat_job_skips_loadJobDetail_after_submit():
    """For chat / chat-codex jobs, submitJob must avoid the
    loadJobDetail flash. Acceptable shapes:

    - A `kind === "chat"` (or startsWith("chat")) check appears BEFORE
      the `await loadJobs()` call, gating an early tab switch so loadJobs
      sees runTab inactive and skips its auto-detail call.
    - OR a kind check directly gates the loadJobDetail call.

    Either way, the source must contain a kind-based branch in or around
    submitJob that mentions both "chat" and the navigation/loadJobDetail
    pathway.
    """
    body = _function_body(_src(), "submitJob")
    # Look for any kind-check that mentions "chat".
    has_kind_check = bool(
        re.search(r'kind\s*===?\s*"chat"', body)
        or re.search(r'kind\.startsWith\(\s*"chat"\s*\)', body)
    )
    assert has_kind_check, (
        "submitJob must branch on `kind` to detect chat jobs (so it can "
        "skip the loadJobDetail flash or switch tabs early)"
    )

    # The chat branch must do tab navigation (data-view=\"terminals\")
    # or explicitly skip loadJobDetail. We accept either; both are
    # listed in the fix spec.
    accepts_either = (
        'data-view="terminals"' in body
        or "loadJobDetail" not in body  # gated away from this fn entirely
        or re.search(r"if\s*\([^)]*kind[^)]*\)[^{]*\{[^}]*loadJobDetail", body)
    )
    assert accepts_either, (
        "submitJob's kind-check must either switch to terminals view "
        "BEFORE loadJobs() runs, or gate loadJobDetail behind a kind check"
    )

    # The tab switch (if used) must come before loadJobs await — i.e.
    # the navBtn.click() should appear before the `await loadJobs()` call
    # in the source order.
    if 'data-view="terminals"' in body:
        nav_pos = body.find("navBtn")
        load_pos = body.find("await loadJobs")
        assert nav_pos != -1, (
            "expected a navBtn handle for the terminals tab switch"
        )
        assert load_pos != -1, "expected `await loadJobs()` in submitJob"
        assert nav_pos < load_pos, (
            "the terminals-tab navigation must happen BEFORE "
            "`await loadJobs()` so the run tab is no longer active when "
            "loadJobs decides whether to call loadJobDetail"
        )
