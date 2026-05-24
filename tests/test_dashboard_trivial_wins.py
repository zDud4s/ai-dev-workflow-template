import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app"


def _src(name):
    return (APP / name).read_text(encoding="utf-8")


def test_auto_select_handles_null_dropped():
    """The buggy destructure default only fires for `undefined`, so a server
    response of `{dropped_candidates: null}` rendered "null dropped (<…>)" to
    the user. Verify the fix uses `||` / null-coalescing instead."""
    src = _src("auto-select.js")
    assert "dropped_candidates: dropped = 0" not in src, (
        "auto-select.js still uses destructure-default for dropped_candidates "
        "which does not protect against null values"
    )
    assert "data.dropped_candidates ||" in src or "data.dropped_candidates ??" in src, (
        "auto-select.js should read dropped_candidates via `||` or `??` to "
        "coerce null to a safe default"
    )


def test_main_uses_promise_all_for_loaders():
    """Five independent loaders were awaited serially on cold boot, taking
    ~5x longer than necessary. They should now be wrapped in Promise.all."""
    src = _src("main.js")
    assert "Promise.all" in src, "main.js should use Promise.all"
    # main.js already had a Promise.all for project/models/memory/etc. The fix
    # adds a SECOND one batching the five loaders. Scan all Promise.all sites
    # and require at least one whose body mentions all five loader names.
    loaders = ("loadEvents", "loadJobs", "loadSessions", "loadSkills", "loadAgents")
    found = False
    for m in re.finditer(r"Promise\.all", src):
        window = src[m.start() : m.start() + 500]
        if all(fn in window for fn in loaders):
            found = True
            break
    assert found, (
        "main.js should batch loadEvents/loadJobs/loadSessions/loadSkills/"
        "loadAgents inside a single Promise.all"
    )


def test_core_render_overview_null_guard():
    """renderOverview previously dereferenced $("#overview-cards") without
    a null check, so a stripped/embedded shell of the page would throw on
    `delete overviewCards.dataset.skeletoned`. Verify the entry-point guard."""
    src = _src("core.js")
    # Locate renderOverview's body and check that a guard appears before the
    # `delete overviewCards.dataset.skeletoned` site.
    match = re.search(r"function renderOverview\b[^{]*\{", src)
    assert match, "renderOverview function definition not found in core.js"
    start = match.end()
    delete_idx = src.index("delete overviewCards.dataset.skeletoned", start)
    body_before_delete = src[start:delete_idx]
    guard_present = (
        'if (!$("#overview-cards"))' in body_before_delete
        or "if (overviewCards == null)" in body_before_delete
        or "if (!overviewCards)" in body_before_delete
    )
    assert guard_present, (
        "renderOverview should guard against a missing #overview-cards element "
        "before dereferencing it (e.g. `if (!$(\"#overview-cards\")) return;`)"
    )
