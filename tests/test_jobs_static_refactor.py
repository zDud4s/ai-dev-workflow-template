import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / ".ai" / "dashboard" / "app"
JOBS_JS = APP / "jobs.js"
CORE_JS = APP / "core.js"
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"


def _src():
    return JOBS_JS.read_text(encoding="utf-8")


def _read(path):
    return path.read_text(encoding="utf-8")


def _function_body(src, name):
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


def test_no_inline_onclick_cancel():
    assert 'onclick="cancelJob(' not in _src(), \
        "inline onclick=cancelJob should be removed; use data-job-cancel + delegation"


def test_data_job_cancel_attr_present():
    assert "data-job-cancel=" in _src(), \
        "cancel button must carry data-job-cancel attribute"


def test_no_per_item_click_listener_in_loadjobs():
    src = _src()
    pat = re.compile(r'\.list-item[^\n]*\n[^\n]*addEventListener\("click"', re.MULTILINE)
    assert pat.search(src) is None, \
        "per-item click listener attach inside list render should be replaced by delegation"


def test_in_flight_guard_exists():
    assert "_jobsLoadInFlight" in _src(), \
        "loadJobs must use an in-flight guard against visibilitychange races"


def test_ev_search_debounced():
    src = _src()
    pat = re.compile(
        r'id === "ev-search".{0,240}clearTimeout\(_evSearchTimer\).{0,240}'
        r'setTimeout\(\(\) => \{.{0,240}renderEvents\(\);.{0,80}\}, 150\)',
        re.S,
    )
    assert pat.search(src), \
        "#ev-search input handler must debounce renderEvents by 150ms"


def test_dispatch_toggle_disabled_until_loaded():
    jobs = _src()
    toggle_body = _function_body(jobs, "toggleDispatchMode")
    guard_idx = toggle_body.find("!window._modelsCache")
    dataset_idx = toggle_body.find("btn.dataset.current")
    assert guard_idx != -1, "toggleDispatchMode must guard on window._modelsCache"
    assert dataset_idx != -1 and guard_idx < dataset_idx, (
        "toggleDispatchMode should check _modelsCache before reading btn.dataset.current"
    )
    assert re.search(
        r'setMsg\(\s*"#toast-root"\s*,\s*"warn"\s*,\s*"Models still loading',
        toggle_body,
    ), "the unloaded guard should route feedback through the toast helper"

    core = _read(CORE_JS)
    render_body = _function_body(core, "renderModels")
    cache_idx = render_body.find("_modelsCache = models")
    enable_idx = render_body.find('$("#dispatch-toggle")?.removeAttribute("disabled")')
    assert cache_idx != -1 and enable_idx != -1 and cache_idx < enable_idx, (
        "renderModels should enable #dispatch-toggle after _modelsCache is populated"
    )

    index = _read(INDEX_HTML)
    assert re.search(
        r'<button\b(?=[^>]*\bid="dispatch-toggle")(?=[^>]*\bdisabled\b)[^>]*>',
        index,
    ), "initial #dispatch-toggle markup should include disabled"


def test_jobs_list_early_exit_on_same_length():
    src = _src()
    len_idx = src.find("el.children.length === rows.length")
    materialize_idx = src.find("Array.from(el.children)")
    assert len_idx != -1, \
        "jobs list reconciliation must check el.children.length before materializing children"
    assert materialize_idx != -1, \
        "jobs list reconciliation should keep a materialized fallback for non-list children"
    assert len_idx < materialize_idx, \
        "same-length fast path must run before Array.from(el.children)"


def test_cancel_job_string_guard_and_button_disable():
    body = _function_body(_src(), "cancelJob")
    assert "String(jobId).slice(0, 8)" in body, \
        "cancelJob confirm preview must guard non-string job ids with String(jobId)"
    assert re.search(r"\bdisabled\s*=\s*true", body), \
        "cancelJob must disable the triggering cancel button while the request is in flight"
    assert re.search(r"\bdisabled\s*=\s*false", body) and "finally" in body, \
        "cancelJob must re-enable the triggering cancel button in finally"
