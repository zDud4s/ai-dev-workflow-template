"""Batch 7 fixes for .ai/dashboard/app/{core.js,main.js,jobs.js}.

Sweep of residual MEDIUM null-deref / silent-swallow issues from the
bug-hunt report (see docs/bug-hunt-status.md "MEDIUM ainda abertos" /
core.js + main.js / jobs.js sections).  All assertions are static regex
pins so the suite is jsdom-free.

Fixes covered:

core.js
  - loadTokenUsage `if (!r.ok) return;` was silent; now console.warn-ed
    with the failing path so operators see why "—" cards stick.
  - Codex `last_event_at` toLocaleString swallow now console.warn-ed.

main.js
  - Each post-YAML count target (#project-name, #count-memory,
    #count-plans, #count-specs) extracted to a local and null-guarded
    before the `.textContent =` write.

jobs.js
  - loadJobs early-returns when #jobs-list is missing (previous shape
    null-derefed `el.dataset.skeletoned` immediately after the
    aria-live attribute set was guarded).
  - $("#view-run")/$("#view-terminals").classList accesses use optional
    chaining so a stripped shell doesn't abort the poll-scheduler.
  - toggleDispatchMode early-returns when #dispatch-toggle is absent.
  - submitJob bails when required form inputs are missing.
  - clearEvents catch null-guards #events-meta write.
  - loadTimeline bails when #timeline-chart missing; #count-timeline
    write is null-guarded.
  - loadEvents bails when #events-body missing; #count-events writes
    are null-guarded in both branches.
  - loadJobDetail resolves #jobs-doc once + bails when missing.
  - loadJobs catch null-guards #jobs-list write.
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


def test_load_token_usage_warns_on_http_failure() -> None:
    """`if (!r.ok) return;` was silent — operators saw stuck "—" cards
    with no diagnostic.  Verify the branch now console.warns the path."""
    body = _function_body(_src("core.js"), "loadTokenUsage")
    m = re.search(
        r"if\s*\(\s*!r\.ok\s*\)\s*\{([^}]*)\}",
        body,
        flags=re.DOTALL,
    )
    assert m is not None, (
        "loadTokenUsage must wrap the `if (!r.ok)` branch in a block "
        "instead of the previous silent `return;` shape"
    )
    inner = m.group(1)
    assert "console.warn" in inner, (
        "loadTokenUsage non-2xx branch must console.warn the failure "
        "(silent return left operators stuck on `—` with zero diagnostic)"
    )
    assert "/api/usage/total" in inner, (
        "console.warn should mention the failing path so log triage is useful"
    )


def test_codex_last_event_format_swallow_now_warns() -> None:
    """The codex `last_event_at` toLocaleString swallow used to be
    `/* ignore */` — replace with a console.warn so operators can
    correlate missing "Codex seen …" strings with the bad timestamp."""
    body = _function_body(_src("core.js"), "loadTokenUsage")
    # The exact toLocaleString try-block must still exist.
    assert "toLocaleString()" in body
    # Find the codex.last_event_at try and verify the catch warns instead
    # of silently ignoring.
    m = re.search(
        r"if\s*\(\s*codexRL\.last_event_at\s*\)\s*\{(.+?)\}\s*\}",
        body,
        flags=re.DOTALL,
    )
    assert m is not None, (
        "loadTokenUsage must keep the `if (codexRL.last_event_at)` guard"
    )
    inner = m.group(1)
    assert "console.warn" in inner, (
        "codex last_event_at format failure must console.warn instead of "
        "the previous /* ignore */ swallow"
    )


# ===== main.js =====================================================


def test_main_project_name_textcontent_guarded() -> None:
    """Unguarded `$("#project-name").textContent = …` aborted the rest of
    loadAll() if the markup was stripped — overview cards / activity / lists
    would never render."""
    src = _src("main.js")
    assert '$("#project-name").textContent' not in src, (
        'main.js must not have unguarded `$("#project-name").textContent = …` '
        "— extract to a local and null-guard"
    )
    assert re.search(
        r"const\s+projectNameEl\s*=\s*\$\("
        r'"#project-name"\)\s*;\s*if\s*\(\s*projectNameEl\s*\)\s*projectNameEl\.textContent',
        src,
    ), (
        "main.js must extract #project-name into projectNameEl and null-guard "
        "the .textContent write"
    )


def test_main_count_targets_each_guarded() -> None:
    """#count-memory / #count-plans / #count-specs each need their own
    null-guard.  JS halts on the first throw, so a single missing element
    would abort the whole sequence (overview cards rendering, loadTokenUsage,
    renderActivity, etc.)."""
    src = _src("main.js")
    body = _function_body(src, "loadAll")
    for sel, var in (
        ("#count-memory", "countMemoryEl"),
        ("#count-plans", "countPlansEl"),
        ("#count-specs", "countSpecsEl"),
    ):
        assert ('$("' + sel + '").textContent =') not in body, (
            "loadAll must not have unguarded `$(\""
            + sel
            + "\").textContent = …` — extract + null-guard"
        )
        assert re.search(
            r"const\s+" + var + r"\s*=\s*\$\("
            r'"' + sel + r'"\)\s*;\s*if\s*\(\s*' + var + r"\s*\)\s*" + var + r"\.textContent",
            body,
        ), (
            "loadAll must null-guard "
            + sel
            + " (extract into "
            + var
            + " and gate the .textContent write)"
        )


# ===== jobs.js =====================================================


def test_load_jobs_early_returns_when_list_missing() -> None:
    """Every later branch derefs `el` (.dataset, .innerHTML, .children,
    .addEventListener) — bail when #jobs-list is missing instead of
    null-derefing on the next `delete el.dataset.skeletoned`."""
    body = _function_body(_src("jobs.js"), "loadJobs")
    # The early-return must come before the dataset write.  Strip comments
    # first so a comment mentioning "delete el.dataset.skeletoned" doesn't
    # confuse the position search.
    code = re.sub(r"//[^\n]*", "", body)
    idx = code.find("delete el.dataset.skeletoned")
    assert idx != -1, "loadJobs must still clear el.dataset.skeletoned"
    # Find the `if (!el) return;` guard — scan all matches and pick the
    # one immediately preceding the dataset write.
    guards = [m for m in re.finditer(r"if\s*\(\s*!\s*el\s*\)\s*return", code)]
    assert guards, (
        "loadJobs must add `if (!el) return;` somewhere in the body "
        "to guard the `delete el.dataset.skeletoned` write"
    )
    preceding = [g for g in guards if g.end() < idx]
    assert preceding, (
        "The `if (!el) return` guard must appear BEFORE the "
        "`delete el.dataset.skeletoned` line — otherwise a missing "
        "#jobs-list null-derefs immediately"
    )


def test_load_jobs_view_class_lookups_optional_chained() -> None:
    """`$("#view-run").classList.contains("active")` and
    `$("#view-terminals").classList.contains("active")` previously
    null-derefed when either element was missing.  Verify both use
    optional chaining."""
    body = _function_body(_src("jobs.js"), "loadJobs")
    # The exact unguarded forms must be gone.
    assert '$("#view-run").classList' not in body, (
        'loadJobs must use optional chaining: `$("#view-run")?.classList…`'
    )
    assert '$("#view-terminals").classList' not in body, (
        'loadJobs must use optional chaining: `$("#view-terminals")?.classList…`'
    )
    # And the optional-chained shape must exist.
    assert re.search(
        r'\$\("#view-run"\)\?\.classList\.contains',
        body,
    ), "loadJobs must call `$(\"#view-run\")?.classList.contains(…)`"
    assert re.search(
        r'\$\("#view-terminals"\)\?\.classList\.contains',
        body,
    ), "loadJobs must call `$(\"#view-terminals\")?.classList.contains(…)`"


def test_toggle_dispatch_mode_guards_missing_button() -> None:
    """`btn.dataset.current` and `btn.disabled = true` previously
    null-derefed when #dispatch-toggle was missing."""
    body = _function_body(_src("jobs.js"), "toggleDispatchMode")
    # Bail-out must come before any .dataset / .disabled access.
    bail = re.search(r"if\s*\(\s*!\s*btn\s*\)\s*return", body)
    assert bail is not None, (
        "toggleDispatchMode must early-return when #dispatch-toggle is missing"
    )
    dataset_pos = body.find("btn.dataset.current")
    assert dataset_pos != -1, "toggleDispatchMode must still read btn.dataset.current"
    assert bail.end() < dataset_pos, (
        "The `if (!btn) return` guard must run BEFORE `btn.dataset.current` "
        "is accessed"
    )


def test_submit_job_guards_required_form_inputs() -> None:
    """submitJob used to call `$("#run-kind").value` and
    `$("#run-task").value.trim()` after only resolving #run-submit.  A
    missing input element would null-deref `.value` and abort."""
    body = _function_body(_src("jobs.js"), "submitJob")
    assert '$("#run-kind").value' not in body, (
        'submitJob must not call `.value` on a fresh `$("#run-kind")` lookup '
        "— extract to kindEl and null-guard"
    )
    assert '$("#run-task").value' not in body, (
        'submitJob must not call `.value` on a fresh `$("#run-task")` lookup '
        "— extract to taskEl and null-guard"
    )
    assert re.search(
        r"const\s+kindEl\s*=\s*\$\(\s*[\"']#run-kind[\"']\s*\)\s*;\s*"
        r"const\s+taskEl\s*=\s*\$\(\s*[\"']#run-task[\"']\s*\)\s*;",
        body,
    ), "submitJob must resolve kindEl + taskEl up-front"
    assert re.search(
        r"if\s*\(\s*!\s*btn\s*\|\|\s*!\s*kindEl\s*\|\|\s*!\s*taskEl\s*\)\s*return",
        body,
    ), "submitJob must null-guard btn + kindEl + taskEl together before any .value read"


def test_clear_events_catch_guards_meta_write() -> None:
    """`$("#events-meta").textContent = …` inside the clearEvents catch
    block previously masked the underlying error with a fresh TypeError
    when the element was missing."""
    body = _function_body(_src("jobs.js"), "clearEvents")
    assert '$("#events-meta").textContent' not in body, (
        'clearEvents catch must not have unguarded `$("#events-meta").textContent`'
    )
    # Pattern: resolve, then null-guard.
    assert re.search(
        r"const\s+meta\s*=\s*\$\(\s*[\"']#events-meta[\"']\s*\)\s*;\s*"
        r"if\s*\(\s*meta\s*\)\s*meta\.textContent",
        body,
    ), "clearEvents catch must resolve #events-meta into a local and null-guard"


def test_load_timeline_bails_when_chart_missing() -> None:
    """loadTimeline derefs `chart.dataset` / `chart.innerHTML` in every
    branch — bail when #timeline-chart is stripped from markup."""
    body = _function_body(_src("jobs.js"), "loadTimeline")
    # Bail-out comes before renderTimelineSkeletons() — assert ordering.
    bail = re.search(r"if\s*\(\s*!\s*chart\s*\)\s*return", body)
    assert bail is not None, (
        "loadTimeline must early-return when #timeline-chart is missing"
    )
    skeleton_pos = body.find("renderTimelineSkeletons()")
    assert skeleton_pos != -1
    assert bail.end() < skeleton_pos, (
        "The `if (!chart) return` guard must run before renderTimelineSkeletons()"
    )
    # The #count-timeline write must also be null-guarded.
    assert '$("#count-timeline").textContent' not in body, (
        'loadTimeline must not have unguarded `$("#count-timeline").textContent`'
    )


def test_load_events_bails_when_body_missing() -> None:
    """loadEvents derefs `body.innerHTML` / `body.dataset` everywhere — early-
    return when #events-body is missing so the catch block doesn't mask
    a load failure with a fresh TypeError."""
    body = _function_body(_src("jobs.js"), "loadEvents")
    assert re.search(r"if\s*\(\s*!\s*body\s*\)\s*return", body), (
        "loadEvents must early-return when #events-body is missing"
    )
    # Both #count-events writes must be guarded — neither uses the raw
    # `$("#count-events").textContent =` form anymore.
    assert '$("#count-events").textContent' not in body, (
        'loadEvents must not have any unguarded `$("#count-events").textContent` writes'
    )


def test_load_job_detail_resolves_doc_once_and_guards() -> None:
    """loadJobDetail previously called `$("#jobs-doc").innerHTML` in three
    places — a missing element would abort whichever branch ran first."""
    body = _function_body(_src("jobs.js"), "loadJobDetail")
    # The early-return must exist.
    bail = re.search(r"if\s*\(\s*!\s*docEl\s*\)\s*return", body)
    assert bail is not None, (
        "loadJobDetail must early-return when #jobs-doc is missing"
    )
    # No remaining unguarded `$("#jobs-doc").innerHTML` lookups.
    assert '$("#jobs-doc").innerHTML' not in body, (
        'loadJobDetail must reuse docEl instead of looking up #jobs-doc again'
    )


def test_load_jobs_catch_guards_jobs_list_write() -> None:
    """loadJobs catch block wrote into #jobs-list unguarded.  A missing
    element would mask the real error with a TypeError."""
    body = _function_body(_src("jobs.js"), "loadJobs")
    # Locate the catch block source range (best-effort: from `} catch (e) {`
    # to the next top-level `} finally {`).
    catch_idx = body.find("} catch (e) {")
    assert catch_idx != -1, "loadJobs must still have a catch block"
    finally_idx = body.find("} finally {", catch_idx)
    catch_body = body[catch_idx:finally_idx] if finally_idx != -1 else body[catch_idx:]
    assert '$("#jobs-list").innerHTML' not in catch_body, (
        'loadJobs catch block must not have unguarded `$("#jobs-list").innerHTML`'
    )
    assert re.search(
        r"const\s+jobsListEl\s*=\s*\$\(\s*[\"']#jobs-list[\"']\s*\)\s*;\s*"
        r"if\s*\(\s*jobsListEl\s*\)\s*jobsListEl\.innerHTML",
        catch_body,
    ), (
        "loadJobs catch must resolve #jobs-list into a local and null-guard "
        "the .innerHTML write"
    )


# ===== Verification of earlier-batch fixes (regression guards) =====


def test_count_memory_entries_coerces_null() -> None:
    """Verify the batch-4 defensive coerce is still present."""
    body = _function_body(_src("core.js"), "countMemoryEntries")
    assert re.search(r'text\s*=\s*text\s*\|\|\s*""', body), (
        "countMemoryEntries must keep the `text = text || \"\"` coerce"
    )


def test_format_tokens_empty_string_returns_emdash() -> None:
    """Verify the batch-4 explicit empty-string guard."""
    body = _function_body(_src("core.js"), "formatTokens")
    assert 'n === ""' in body, (
        "formatTokens must keep the explicit empty-string guard"
    )


def test_dec_date_null_guarded() -> None:
    """Verify the batch-4 #dec-date null-guard."""
    src = _src("core.js")
    assert '$("#dec-date").value = today' not in src, (
        "Unguarded #dec-date write must remain absent"
    )
    assert re.search(
        r'const\s+decDate\s*=\s*\$\("#dec-date"\)\s*;\s*if\s*\(\s*decDate\s*\)',
        src,
    ), "Batch-4 #dec-date null-guard must remain intact"


def test_relative_time_negative_dur_guarded() -> None:
    """Verify the batch-5 Math.max(0, ...) clamp on relativeTime stays."""
    body = _function_body(_src("jobs.js"), "relativeTime")
    assert re.search(r"Math\.max\(\s*0\s*,", body), (
        "relativeTime must keep the Math.max(0, ...) negative-duration clamp"
    )


def test_visibilitychange_debounce_dedupe_still_active() -> None:
    """Verify the batch-5 visibilitychange double-fire debounce stays."""
    src = _src("jobs.js")
    assert re.search(r"_lastVisibilityState\s*===\s*state\s*&&", src), (
        "visibilitychange dedupe (state-equal AND inside-window) must remain"
    )


def test_jobs_safe_tool_whitelist_still_active() -> None:
    """Verify the _jobsSafeTool whitelist gates every pillTool call site."""
    src = _src("jobs.js")
    assert "_jobsSafeTool" in src, "_jobsSafeTool whitelist must still exist"
    # Find all pillTool( call sites and confirm each routes through _jobsSafeTool.
    pill_calls = re.findall(r"pillTool\(([^)]+)\)", src)
    assert pill_calls, "jobs.js must still call pillTool() somewhere"
    for arg in pill_calls:
        assert "_jobsSafeTool" in arg, (
            "pillTool() call site must route through _jobsSafeTool(): " + arg
        )
