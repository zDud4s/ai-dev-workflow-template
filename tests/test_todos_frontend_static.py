import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / ".ai" / "dashboard" / "index.html"
TODOS_JS = ROOT / ".ai" / "dashboard" / "app" / "todos.js"


def test_todos_view_registered_between_memory_and_decisions():
    src = INDEX_HTML.read_text(encoding="utf-8")
    assert src.index('id="tab-memory"') < src.index('id="tab-todos"') < src.index('id="tab-decisions"')
    assert src.index('id="view-memory"') < src.index('id="view-todos"') < src.index('id="view-decisions"')


def test_todos_js_purifies_title():
    src = TODOS_JS.read_text(encoding="utf-8")
    assert "DOMPurify.sanitize" in src

    lines = src.splitlines()
    for match in re.finditer(r"\binnerHTML\s*=", src):
        line_no = src.count("\n", 0, match.start())
        context = "\n".join(lines[max(0, line_no - 3):line_no + 1])
        assert "DOMPurify.sanitize" in context
