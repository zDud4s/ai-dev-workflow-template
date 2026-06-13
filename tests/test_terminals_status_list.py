from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_terminals_tab_has_no_layout_mode_buttons():
    """Chunk 5b-1: the Terminals tab is a pure status list. The inline
    multi-pane layout selector is gone — index.html's Terminals section must
    no longer carry the split/grid layout buttons or their group wrapper."""
    index_html = (ROOT / ".ai/dashboard/index.html").read_text(encoding="utf-8")
    # Scope the assertion to the Terminals section so an unrelated layout="grid"
    # somewhere else in the document can't mask a regression here.
    start = index_html.find('id="view-terminals"')
    assert start != -1, "Terminals section not found in index.html"
    end = index_html.find("</section>", start)
    section = index_html[start:end]

    assert 'data-layout="split"' not in section, (
        "Terminals tab must not carry the split layout button"
    )
    assert 'data-layout="grid"' not in section, (
        "Terminals tab must not carry the grid layout button"
    )
    assert "term-layout-group" not in section, (
        "Terminals tab must not carry the layout-mode button group"
    )


def test_terminals_js_has_no_inline_grid_render():
    """The layout machinery (list/split/grid) is removed in 5b-1. terminals.js
    must not reference the grid layout classes or the apply-layout function in
    live code — the canvas owns multi-pane geometry now."""
    src = (ROOT / ".ai/dashboard/app/terminals.js").read_text(encoding="utf-8")

    assert "layout-split" not in src, (
        "terminals.js must not reference the layout-split grid class"
    )
    assert "layout-grid" not in src, (
        "terminals.js must not reference the layout-grid grid class"
    )
    assert "termApplyLayout" not in src, (
        "terminals.js must not reference termApplyLayout (layout machinery removed)"
    )


def test_status_row_has_send_to_canvas_control():
    """Each status row emits a send-to-canvas control, and the on-canvas state is
    shown ON that control (selected look) rather than as a separate text badge."""
    src = (ROOT / ".ai/dashboard/app/terminals.js").read_text(encoding="utf-8")

    assert "term-status-row" in src, (
        "terminals.js must build status rows (term-status-row)"
    )
    assert "send-to-canvas" in src, (
        "status rows must carry the .send-to-canvas control"
    )
    # The standalone "on canvas" text badge was removed; the on-canvas state is
    # toggled on the ⊞ button itself (a .on-canvas class → selected styling).
    assert "on-canvas-badge" not in src, (
        "the standalone on-canvas text badge must be gone"
    )
    assert "btn.classList.toggle(\"on-canvas\"" in src or "_markRowOnCanvas" in src, (
        "on-canvas state must be reflected on the send-to-canvas button"
    )


def test_canvas_html_loads_panecore_and_engine():
    html = (ROOT / ".ai/dashboard/app/canvas.html").read_text(encoding="utf-8")

    # The canvas page must load its scripts sibling-relative (it lives under
    # app/, so srcs are "./<file>.js") and in dependency order: the shared
    # engine (core/skills), then PaneCore, then the canvas-specific engine
    # (split-tree, canvas-bus) and finally the boot file canvas.js.
    # pane-helpers.js (the pure render leaves PaneCore depends on) MUST load
    # before pane-core.js — pane-core.js is the isolated canvas renderer and
    # resolves termSetPillState / termRefreshCost / termExportMarkdown /
    # renderBashCommand / termPtyWsUrl etc. as globals from pane-helpers.js.
    order = [
        "core.js",
        "skills.js",
        "pane-helpers.js",
        "pane-core.js",
        "split-tree.js",
        "canvas-bus.js",
        "canvas.js",
    ]
    indices = []
    for name in order:
        # Look for a <script ... src referencing this file. Restrict to the
        # script-src form so a stray mention elsewhere can't satisfy it.
        needle = 'src="./' + name + '"'
        idx = html.find(needle)
        assert idx != -1, f"{name} not loaded via {needle}"
        indices.append(idx)

    assert indices == sorted(indices), (
        "script tags out of order: " + repr(list(zip(order, indices)))
    )


def test_terminals_has_send_to_canvas_affordance():
    """terminals.js must wire a per-pane "send to canvas" control that opens the
    canvas window and talks to it over CanvasBus. Source-level only (no browser):
    we assert the affordance class/data-action, the named-window open of
    app/canvas.html, and at least one CanvasBus reference."""
    src = (ROOT / ".ai/dashboard/app/terminals.js").read_text(encoding="utf-8")

    assert '"send-canvas"' in src, "missing send-canvas data-action hook"
    assert "send-to-canvas" in src, "missing .send-to-canvas affordance class"
    # The canvas window is opened (by absolute URL) into the named "dash-canvas"
    # window via canvasOpenWindow.
    assert '"dash-canvas"' in src, "missing named canvas window 'dash-canvas'"
    assert "app/canvas.html" in src, "missing the canvas page url"
    assert "canvasOpenWindow" in src, (
        "send-to-canvas must open the canvas via canvasOpenWindow"
    )
    assert "CanvasBus" in src, "terminals.js must reference window.CanvasBus"
    # The bus client must be created via CanvasBus.create + a queue for the
    # open-before-ready race.
    assert "CanvasBus.create" in src or "window.CanvasBus.create" in src, (
        "terminals.js must create a CanvasBus client"
    )


def test_index_html_drops_pane_core_keeps_helpers_and_bus():
    """The dashboard uses its own pane model (not PaneCore), so index.html must
    NOT load app/pane-core.js. It must still load pane-helpers.js (terminals.js
    depends on it) and now also canvas-bus.js (the send-to-canvas bridge).
    pane-core.js remains the canvas-only renderer (asserted present in
    canvas.html elsewhere)."""
    index_html = (ROOT / ".ai/dashboard/index.html").read_text(encoding="utf-8")
    canvas_html = (ROOT / ".ai/dashboard/app/canvas.html").read_text(encoding="utf-8")

    assert 'src="app/pane-core.js"' not in index_html, (
        "index.html must not load the canvas-only pane-core.js"
    )
    assert 'src="app/pane-helpers.js"' in index_html, (
        "index.html must keep pane-helpers.js (terminals.js needs it)"
    )
    assert 'src="app/canvas-bus.js"' in index_html, (
        "index.html must load canvas-bus.js for the send-to-canvas bridge"
    )
    # pane-core.js still belongs to the canvas page.
    assert "pane-core.js" in canvas_html, (
        "canvas.html must still load pane-core.js (the isolated renderer)"
    )
