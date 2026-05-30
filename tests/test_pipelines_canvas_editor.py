"""Static regression tests for the dashboard pipeline canvas editor."""
from __future__ import annotations

from html.parser import HTMLParser
import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PIPELINES_JS = REPO_ROOT / ".ai" / "dashboard" / "app" / "pipelines.js"
INDEX_HTML = REPO_ROOT / ".ai" / "dashboard" / "index.html"
STYLES_CSS = REPO_ROOT / ".ai" / "dashboard" / "styles.css"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class _ParentParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str | None] = []
        self.parents: dict[str, list[str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        node_id = attr.get("id")
        if node_id:
            self.parents[node_id] = list(self.stack)
        if tag in self.VOID_TAGS:
            return
        self.stack.append(node_id)

    def handle_endtag(self, tag: str) -> None:
        if self.stack:
            self.stack.pop()


def test_no_list_mode_artifacts() -> None:
    js = _read(PIPELINES_JS)
    html = _read(INDEX_HTML)

    assert not re.search(r"data-mode\s*=\s*['\"]list", js)
    assert "pipeline-add-node" not in js
    assert "pipeline-toggle-view" not in js
    assert "pipeline-add-node" not in html
    assert "pipeline-toggle-view" not in html


def test_canvas_wiring_present() -> None:
    js = _read(PIPELINES_JS)

    assert ".pipeline-canvas" in js
    assert re.search(r"addEventListener\(\s*['\"]pointerdown['\"]", js)
    assert "setPointerCapture" in js
    assert 'data-kind": "out"' in js
    assert 'data-kind": "in"' in js


def test_catalog_drag_is_pointer_based() -> None:
    """Catalog -> canvas uses pointer events, not native HTML5 drag-and-drop.

    Native DnD draws an OS cursor (no-drop / copy) that ignores the CSS
    `cursor`, so the drag could never match the "Targeting HUD" cursor set.
    The pointer-based drag forces the grabbing cursor for the whole drag via a
    body class and renders a custom ghost.
    """
    js = _read(PIPELINES_JS)
    css = _read(STYLES_CSS)

    # Native HTML5 drag-and-drop is gone.
    assert 'draggable: "true"' not in js
    assert not re.search(r"addEventListener\(\s*['\"]dragstart['\"]", js)
    assert not re.search(r"addEventListener\(\s*['\"]dragover['\"]", js)
    assert not re.search(r"addEventListener\(\s*['\"]drop['\"]", js)

    # Pointer drag toggles a body class + shows a ghost.
    assert "pipeline-drag-active" in js
    assert "catalog-drag-ghost" in js

    # Tooltips are suppressed mid-drag: showAgentTooltip bails out while the
    # drag class is set, so hovering other agents doesn't pop their info box.
    assert re.search(
        r"function showAgentTooltip[\s\S]{0,400}?pipeline-drag-active", js
    )

    # CSS forces the HUD cursor globally for the duration of the drag, because
    # pointer capture / drag otherwise bypasses the per-element cursor.
    assert re.search(r"body\.pipeline-drag-active[\s\S]*?--cur-grabbing", css)
    assert re.search(r"body\.pipeline-wire-active[\s\S]*?--cur-crosshair", css)
    assert ".catalog-drag-ghost" in css


def test_modal_inside_pipelines_view() -> None:
    parser = _ParentParser()
    parser.feed(_read(INDEX_HTML))

    parents = parser.parents["pipeline-editor-modal"]
    assert "view-pipelines" in parents
    assert "view-agent-orchestrations" not in parents


def test_inline_node_controls() -> None:
    js = _read(PIPELINES_JS)

    assert "foreignObject" in js
    assert re.search(r'className:\s*["\'][^"\']*dag-node-label', js)
    assert re.search(r'className:\s*["\'][^"\']*dag-node-delete', js)
    assert re.search(r'class:\s*["\']dag-node["\']', js)


def test_yaml_shape_preserved() -> None:
    js = _read(PIPELINES_JS)

    for snippet in (
        'lines.push("description: "',
        'lines.push("output:")',
        'lines.push("  mode: "',
        'lines.push("nodes:")',
        'lines.push("  - id: "',
        'lines.push("    agent: "',
        'lines.push("    depends_on:")',
    ):
        assert snippet in js
