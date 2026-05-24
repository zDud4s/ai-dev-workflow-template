"""A11Y / low-severity fixes for .ai/dashboard/app/jobs.js (batch 4).

Each test asserts a structural invariant in the source so we catch
regressions without needing a JS runtime. The three fixes are:

1. #jobs-list announces dynamic content changes via aria-live (polite).
2. .list-item rows are keyboard-focusable (tabindex + role="button") and
   the delegated listener handles Enter / Space via keydown.
3. The Cancel job button carries an aria-label or title attribute so
   screen readers distinguish multiple "Cancel job" buttons by job id /
   context.
"""

from __future__ import annotations

import re
from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / ".ai" / "dashboard" / "app" / "jobs.js"


def _src() -> str:
    return JOBS_JS.read_text(encoding="utf-8")


# Fix 1 -------------------------------------------------------------------

def test_jobs_list_sets_aria_live():
    """loadJobs must set aria-live on #jobs-list so screen readers
    announce polling updates. Idempotent — guarded behind a
    getAttribute check so we don't re-touch attrs every 2s."""
    src = _src()
    # The setAttribute("aria-live", ...) call must exist.
    assert re.search(r'setAttribute\(\s*"aria-live"', src), (
        "loadJobs must call setAttribute(\"aria-live\", ...) on #jobs-list "
        "so dynamic additions are announced to screen readers"
    )
    # Idempotency guard — getAttribute check before the setAttribute call.
    assert re.search(r'getAttribute\(\s*"aria-live"', src), (
        "aria-live setup must be guarded by a getAttribute check to stay "
        "idempotent across the 2s polling cycle"
    )
    # aria-relevant should accompany aria-live for finer-grained announcements.
    assert re.search(r'setAttribute\(\s*"aria-relevant"', src), (
        "loadJobs should also set aria-relevant (e.g. \"additions\") so "
        "screen readers focus announcements on new rows"
    )


# Fix 2 -------------------------------------------------------------------

def test_list_item_has_tabindex_and_role_button():
    """Row template must include tabindex=\"0\" and role=\"button\"
    so keyboard users can focus and activate job rows."""
    src = _src()
    # Both attrs must appear on the same .list-item template literal.
    # Look for a template string that contains class=\"list-item\" + both attrs.
    list_item_templates = re.findall(
        r'`<div class="list-item"[^`]*`',
        src,
    )
    assert list_item_templates, "could not find any `<div class=\"list-item\" ...` template"
    has_both = any(
        ('tabindex="0"' in t) and ('role="button"' in t)
        for t in list_item_templates
    )
    assert has_both, (
        "the .list-item row template must include both tabindex=\"0\" "
        "and role=\"button\" so keyboard users can focus and activate rows"
    )


def test_jobs_list_handles_keydown():
    """The delegated listener on #jobs-list must also handle keydown so
    Enter / Space activate the focused row, mirroring the click path."""
    src = _src()
    # Tie the keydown handler to the same idempotency guard as click.
    # Heuristic: a keydown addEventListener call must exist somewhere in
    # the file, and Enter / Space must be referenced near it.
    assert re.search(r'addEventListener\(\s*"keydown"', src), (
        "loadJobs delegation must include a keydown listener so keyboard "
        "users can activate focused .list-item rows"
    )
    # The handler must check for Enter or Space.
    keydown_block = src[src.find('addEventListener("keydown"'):]
    assert re.search(r'e\.key\s*(?:!==|===)\s*"Enter"', keydown_block), (
        "keydown handler must check for the Enter key"
    )
    assert re.search(r'e\.key\s*(?:!==|===)\s*" "', keydown_block), (
        "keydown handler must check for the Space key (\" \")"
    )
    # And it must live behind the existing single-wire guard so we don't
    # stack listeners on every poll cycle.
    assert "_jobsListDelegationWired" in src, (
        "keydown listener must reuse _jobsListDelegationWired so the "
        "delegation stays idempotent across polling"
    )


# Fix 3 -------------------------------------------------------------------

def test_cancel_button_has_aria_label_or_title():
    """The cancel-job button must carry an aria-label or title attribute
    so screen readers can distinguish it (multiple Cancel buttons could
    appear if multiple panels are open in future iterations)."""
    src = _src()
    # Find the cancel button template literal.
    # Look for any line containing data-job-cancel=
    cancel_lines = [ln for ln in src.splitlines() if "data-job-cancel=" in ln]
    assert cancel_lines, "expected a data-job-cancel button template in jobs.js"
    # At least one such template must include aria-label= or title=.
    has_a11y = any(
        ("aria-label=" in ln) or ("title=" in ln)
        for ln in cancel_lines
    )
    assert has_a11y, (
        "the Cancel job button must include aria-label= or title= so "
        "screen readers distinguish it (e.g. aria-label=\"Cancel job ${id}\")"
    )
