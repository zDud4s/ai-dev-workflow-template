import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"
TODOS_JS = ROOT / ".ai" / "dashboard" / "app" / "todos.js"


def test_todos_view_registered_in_tasks_section_before_plans():
    src = INDEX_HTML.read_text(encoding="utf-8")
    # Tab lives in the "tasks" nav section, before Plans/Specs/Packets.
    tasks_label = src.index('class="section-label" role="presentation">tasks<')
    tab_todos = src.index('id="tab-todos"')
    tab_plans = src.index('id="tab-plans"')
    assert tasks_label < tab_todos < tab_plans
    # The section element placement is independent of the tab order (tab
    # activates via data-view, not DOM proximity), so we only assert it
    # exists.
    assert 'id="view-todos"' in src


def test_todos_js_purifies_title():
    src = TODOS_JS.read_text(encoding="utf-8")
    assert "DOMPurify.sanitize" in src

    lines = src.splitlines()
    for match in re.finditer(r"\binnerHTML\s*=", src):
        line_no = src.count("\n", 0, match.start())
        context = "\n".join(lines[max(0, line_no - 3):line_no + 1])
        assert "DOMPurify.sanitize" in context
