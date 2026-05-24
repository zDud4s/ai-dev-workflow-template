import re
from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "jobs.js"


def _src():
    return JOBS_JS.read_text(encoding="utf-8")


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
